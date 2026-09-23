"""评估编排服务：批量执行 RAG → RAGAS 指标计算 → 持久化 → 聚合报告。

TECH_DESIGN / AGENTS.md：
- 内置自动化评估工具，支持上传测试集（≥30 条），自动计算上下文相关度与答案正确率。
- 质量门禁：answer_correctness（准确率）≥ 75% 才允许合入 develop。

复用 RagService.retrieve_and_answer（不持久化，避免评估污染聊天记录与缓存），
与 RAGASMetrics（中文适配指标）。

TASK-012 扩展（Dynamic Router 实验能力）：
- 运行支持 Strategy Registry 中的策略名（如 herb_focused）真正驱动检索参数；
  历史策略标签不在注册表中时仍按 Baseline 行为执行，标签原样归档（向后兼容）。
- use_dynamic_router=True 时逐条 Analyzer → Router 选策略，
  逐条策略写入 evaluation_results.retrieval_strategy，run 级记为 dynamic_router，
  配合既有的 by_question_type 即可形成「问题类型 × 策略 × 指标」对比。
  本阶段只建立实验能力，不产出策略优劣结论。

TASK-009 扩展（中医问答评测体系 / Evaluation Baseline）：
- 测试集支持 question_type 分类（herb/prescription/theory/literature/multi_source/
  unanswerable/general）与 dataset_version，为 TASK-011 Query Analyzer / Dynamic
  Router 按问题类型对比检索策略做准备。
- 标准答案强调人工确认：golden_answer 为空或 needs_review=True 的用例只计算
  context_relevancy，answer_correctness 记为 None（未评估），避免把「未标注」
  当成 0 分污染实验结论（AGENTS.md：禁止伪造评测结果）。
- 一次运行落一条 evaluation_runs 归档记录，保存 experiment_name /
  retrieval_strategy / dataset_version / 配置快照 / 聚合指标 / 按问题类型分组指标，
  使 Baseline 与后续消融实验可在同一测试集下对比。
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dynamic_router import DYNAMIC_ROUTER_STRATEGY, ROUTER_VERSION
from src.application.eval_metrics import RAGASMetrics, get_metrics
from src.application.rag_service import RagService
from src.application.retrieval_strategies import (
    RetrievalStrategy,
    get_strategy,
    resolve_retrieval_config,
    strategy_names,
    strategy_out,
)
from src.core.config import settings
from src.core.exceptions import AppException, NotFoundError
from src.domain.models import EvaluationResult, EvaluationRun, TestCase

# ── 问题类型受控词表（TASK-009 建立，阶段十一 Query Analyzer 复用同一份）────
# 词表下沉到 application/question_types.py（避免循环导入），此处重导出，
# 保证既有 import 路径 `from src.application.evaluation_service import QUESTION_TYPES` 不变。
from src.application.question_types import (  # noqa: E402
    QUESTION_TYPES,
    QUESTION_TYPE_LABELS,
)

# Baseline 检索策略标识（TECH_DESIGN §6：HyDE → Dense+Sparse → RRF → Rerank → Gate）
BASELINE_RETRIEVAL_STRATEGY = "hybrid_rrf_rerank_hyde"
DEFAULT_EXPERIMENT_NAME = "baseline"
DEFAULT_DATASET_VERSION = "v1"

# 配置快照中记录的运行时键（不含 API Key 等敏感信息）
_RUNTIME_CONFIG_KEYS = (
    "llm_provider",
    "llm_model",
    "embedding_backend",
    "embedding_model",
    "rerank_backend",
    "rerank_model",
    "hyde_enabled",
    "hyde_backend",
    "hyde_model",
)


@dataclass
class CaseResult:
    """单条测试用例的评估结果。"""

    test_case_id: str
    question: str
    golden_answer: str
    golden_contexts: list[str] = field(default_factory=list)
    answer: str = ""
    retrieved_contexts: list[str] = field(default_factory=list)
    context_relevancy: float = 0.0
    # None 表示未评估（标准答案缺失或待人工确认），与 0.0（已评估且不正确）区分
    answer_correctness: float | None = None
    error: str | None = None  # 该用例评估失败时的原因（仍产出 0 分）
    # TASK-009：问题分类与数据集版本（供按类型分析、实验对比）
    question_type: str = "general"
    dataset_version: str = DEFAULT_DATASET_VERSION
    needs_review: bool = False
    # 阶段十二：该用例实际使用的检索策略（固定策略运行 = 全 run 同一策略；
    # 动态路由运行 = 由 Dynamic Router 按 question_type 逐条选择）
    retrieval_strategy: str | None = None


class EvaluationService:
    """评估编排：测试集 → 批量 RAG 问答 → 指标计算 → 入库 → 聚合。"""

    def __init__(
        self,
        db: AsyncSession,
        rag: RagService | None = None,
        metrics: RAGASMetrics | None = None,
    ) -> None:
        self.db = db
        self.rag = rag or RagService(db)
        self.metrics = metrics or get_metrics()

    # ── 测试集 ──────────────────────────────────────────────────────────────

    async def save_test_set(
        self,
        kb_id: uuid.UUID,
        cases: list[dict],
        dataset_version: str | None = None,
    ) -> list[uuid.UUID]:
        """持久化测试集到 test_cases 表。返回用例 ID 列表。

        TASK-009：支持 question_type / dataset_version / needs_review /
        source_reference。needs_review 未显式给出时按「是否提供了标准答案」推断，
        保持旧有上传行为（给了 golden_answer 即视为已确认）兼容。
        """
        if not cases:
            raise AppException(422, "测试集不能为空")
        ids: list[uuid.UUID] = []
        for c in cases:
            question_type = str(c.get("question_type") or "general").strip() or "general"
            if question_type not in QUESTION_TYPES:
                raise AppException(
                    422,
                    f"未知 question_type={question_type!r}，"
                    f"可选值：{', '.join(QUESTION_TYPES)}",
                )
            version = str(
                c.get("dataset_version") or dataset_version or DEFAULT_DATASET_VERSION
            )
            golden_answer = c.get("golden_answer") or ""
            needs_review = c.get("needs_review")
            if needs_review is None:
                # 未提供标准答案 → 标记需人工确认（不参与 answer_correctness）
                needs_review = not bool(str(golden_answer).strip())
            # 显式生成主键，避免依赖 flush 后回填（部分 async 配置下属性未即时刷新）
            tc_id = uuid.uuid4()
            self.db.add(
                TestCase(
                    id=tc_id,
                    kb_id=kb_id,
                    question=c["question"],
                    golden_answer=golden_answer,
                    golden_contexts=c.get("golden_contexts", []),
                    question_type=question_type,
                    dataset_version=version,
                    needs_review=bool(needs_review),
                    source_reference=str(c.get("source_reference") or ""),
                )
            )
            ids.append(tc_id)
        await self.db.commit()
        logger.info(f"测试集已写入 kb={kb_id} count={len(ids)}")
        return ids

    async def list_test_cases(
        self, kb_id: uuid.UUID, dataset_version: str | None = None
    ) -> list[dict]:
        """列出指定知识库的测试集（供前端查看/人工确认标准答案）。"""
        stmt = (
            select(TestCase)
            .where(TestCase.kb_id == kb_id)
            .order_by(TestCase.question_type.asc(), TestCase.created_at.asc())
        )
        if dataset_version:
            stmt = stmt.where(TestCase.dataset_version == dataset_version)
        rows = (await self.db.scalars(stmt)).all()
        return [self._case_out(tc) for tc in rows]

    async def confirm_test_case(
        self,
        case_id: uuid.UUID,
        golden_answer: str | None = None,
        golden_contexts: list[str] | None = None,
        source_reference: str | None = None,
        question_type: str | None = None,
        needs_review: bool | None = None,
    ) -> dict:
        """人工确认/修订单条用例的标准答案（TASK-009 录入机制）。

        提供 golden_answer 时默认将 needs_review 置为 False（已确认），
        显式传入 needs_review 则以显式值为准。
        """
        tc = await self.db.get(TestCase, case_id)
        if tc is None:
            raise NotFoundError("测试用例不存在")
        if golden_answer is not None:
            tc.golden_answer = golden_answer
        if golden_contexts is not None:
            tc.golden_contexts = golden_contexts
        if source_reference is not None:
            tc.source_reference = source_reference
        if question_type is not None:
            if question_type not in QUESTION_TYPES:
                raise AppException(
                    422,
                    f"未知 question_type={question_type!r}，可选值：{', '.join(QUESTION_TYPES)}",
                )
            tc.question_type = question_type
        if needs_review is None:
            # 显式提供了标准答案即视为已确认；否则保持原值
            if golden_answer is not None and golden_answer.strip():
                tc.needs_review = False
        else:
            tc.needs_review = bool(needs_review)
        await self.db.commit()
        await self.db.refresh(tc)
        return self._case_out(tc)

    @staticmethod
    def _case_out(tc: TestCase) -> dict:
        return {
            "id": str(tc.id),
            "kb_id": str(tc.kb_id),
            "question": tc.question,
            "golden_answer": tc.golden_answer,
            "golden_contexts": tc.golden_contexts or [],
            "question_type": tc.question_type,
            "question_type_label": QUESTION_TYPE_LABELS.get(
                tc.question_type, tc.question_type
            ),
            "dataset_version": tc.dataset_version,
            "needs_review": tc.needs_review,
            "source_reference": tc.source_reference,
            "created_at": tc.created_at.isoformat() if tc.created_at else None,
        }

    # ── 评估运行 ────────────────────────────────────────────────────────────

    async def run(
        self,
        kb_id: uuid.UUID,
        case_ids: list[uuid.UUID] | None = None,
        experiment_name: str = DEFAULT_EXPERIMENT_NAME,
        retrieval_strategy: str = BASELINE_RETRIEVAL_STRATEGY,
        dataset_version: str | None = None,
        use_dynamic_router: bool = False,
    ) -> tuple[EvaluationRun, list[CaseResult]]:
        """运行评估。返回 (EvaluationRun 归档记录, per-case results)。

        - 从 test_cases 表加载用例（按 kb_id；可选 case_ids 过滤）
        - 逐条调用 RAG 检索+生成，计算 RAGAS 指标
          （标准答案未确认的用例只算 context_relevancy）
        - 写入 evaluation_results（带 run_id 与实验维度）+ evaluation_runs 归档

        阶段十二（实验能力，不产出任何策略优劣结论）：
        - retrieval_strategy 传入 Strategy Registry 中已注册的策略名
          （baseline_hybrid / herb_focused / ...）时，本次运行按该策略参数检索；
          传入阶段九历史标签（如 hybrid_rrf_rerank_hyde）则不在注册表中，
          按既有 Baseline 行为执行，标签照原样归档，保证历史实验不受影响。
        - use_dynamic_router=True：逐条走 Query Analyzer + Dynamic Router，
          每条用例记录其实际使用的策略到 evaluation_results.retrieval_strategy，
          run 级 retrieval_strategy 记为 dynamic_router，便于与 Baseline 对比。
        """
        cases = await self._load_cases(kb_id, case_ids)
        if not cases:
            raise AppException(404, "未找到测试用例：请先上传测试集")

        version = dataset_version or self._infer_dataset_version(cases)
        strategy_label = retrieval_strategy or BASELINE_RETRIEVAL_STRATEGY
        if use_dynamic_router:
            strategy_label = DYNAMIC_ROUTER_STRATEGY
        # 固定策略（非动态路由）：注册表中的策略才真正驱动检索参数
        fixed_strategy: RetrievalStrategy | None = (
            None if use_dynamic_router else get_strategy(retrieval_strategy)
        )
        config_snapshot = await self._config_snapshot(
            strategy_label, strategy=fixed_strategy, dynamic_router=use_dynamic_router
        )

        run = EvaluationRun(
            kb_id=kb_id,
            experiment_name=experiment_name or DEFAULT_EXPERIMENT_NAME,
            retrieval_strategy=strategy_label,
            dataset_version=version,
            config_snapshot=config_snapshot,
        )
        self.db.add(run)
        await self.db.flush()  # 取 run.id 供 evaluation_results.run_id 引用

        results: list[CaseResult] = []
        for tc in cases:
            res = await self._evaluate_case(
                kb_id, tc, strategy=fixed_strategy, dynamic_router=use_dynamic_router
            )
            res.dataset_version = tc.dataset_version
            if not use_dynamic_router:
                res.retrieval_strategy = strategy_label
            results.append(res)
            # 持久化评估结果（失败用例也留存，便于审计）
            self.db.add(
                EvaluationResult(
                    test_case_id=tc.id,
                    retrieved_contexts=res.retrieved_contexts,
                    context_relevancy=res.context_relevancy,
                    answer_correctness=res.answer_correctness,
                    run_id=str(run.id),
                    experiment_name=run.experiment_name,
                    # 阶段十二：动态路由运行记录逐条实际策略；否则记录 run 级策略
                    retrieval_strategy=res.retrieval_strategy or run.retrieval_strategy,
                    dataset_version=tc.dataset_version,
                    question_type=tc.question_type,
                    answer=res.answer,
                    error=res.error,
                )
            )

        cr, ac, evaluated, skipped, passed = summarize(results)
        run.case_count = len(results)
        run.evaluated_count = evaluated
        run.skipped_count = skipped
        run.context_relevancy = cr
        run.answer_correctness = ac
        run.passed = passed
        run.threshold = settings.EVAL_ACCURACY_THRESHOLD
        run.by_question_type = breakdown_by_question_type(results)
        await self.db.commit()
        logger.info(
            f"评估完成 run_id={run.id} experiment={run.experiment_name} "
            f"strategy={run.retrieval_strategy} dataset={run.dataset_version} "
            f"kb={kb_id} cases={len(results)} evaluated={evaluated} skipped={skipped} "
            f"cr={cr:.3f} ac={'None' if ac is None else f'{ac:.3f}'}"
        )
        return run, results

    @staticmethod
    def list_strategies() -> list[dict]:
        """阶段十二：可用检索策略清单（含生效参数）。

        供 Experiment A（Baseline）与 Experiment B（固定策略 / 动态路由）对比时选择，
        也便于确认不同策略的参数确实存在差异。
        """
        return [strategy_out(name) for name in strategy_names()]

    async def list_runs(self, kb_id: uuid.UUID | None = None) -> list[dict]:
        """实验运行归档列表（按时间倒序），用于 Baseline 与消融实验对比。"""
        stmt = select(EvaluationRun).order_by(EvaluationRun.created_at.desc()).limit(100)
        if kb_id is not None:
            stmt = stmt.where(EvaluationRun.kb_id == kb_id)
        rows = (await self.db.scalars(stmt)).all()
        return [self._run_out(r) for r in rows]

    async def get_run(self, run_id: uuid.UUID) -> dict:
        run = await self.db.get(EvaluationRun, run_id)
        if run is None:
            raise NotFoundError("评估运行不存在")
        return self._run_out(run)

    @staticmethod
    def _run_out(run: EvaluationRun) -> dict:
        return {
            "run_id": str(run.id),
            "kb_id": str(run.kb_id),
            "experiment_name": run.experiment_name,
            "retrieval_strategy": run.retrieval_strategy,
            "dataset_version": run.dataset_version,
            "case_count": run.case_count,
            "evaluated_count": run.evaluated_count,
            "skipped_count": run.skipped_count,
            "context_relevancy": run.context_relevancy,
            "answer_correctness": run.answer_correctness,
            "passed": run.passed,
            "threshold": run.threshold,
            "config_snapshot": run.config_snapshot or {},
            "by_question_type": run.by_question_type or [],
            "created_at": run.created_at.isoformat() if run.created_at else None,
        }

    async def list_history(
        self, kb_id: uuid.UUID | None = None, run_id: str | None = None
    ) -> list[dict]:
        """历史评估结果（关联 test_case 取问题与标准答案）。"""
        stmt = (
            select(EvaluationResult, TestCase)
            .join(TestCase, TestCase.id == EvaluationResult.test_case_id)
            .order_by(EvaluationResult.created_at.desc())
            .limit(200)
        )
        if kb_id is not None:
            stmt = stmt.where(TestCase.kb_id == kb_id)
        if run_id:
            stmt = stmt.where(EvaluationResult.run_id == run_id)
        rows = (await self.db.execute(stmt)).all()
        return [
            {
                "id": str(er.id),
                "run_id": er.run_id,
                "question": tc.question,
                "question_type": er.question_type,
                "question_type_label": QUESTION_TYPE_LABELS.get(
                    er.question_type, er.question_type
                ),
                "experiment_name": er.experiment_name,
                "retrieval_strategy": er.retrieval_strategy,
                "dataset_version": er.dataset_version,
                "golden_answer": tc.golden_answer,
                "answer": er.answer,
                "answer_correctness": er.answer_correctness,
                "context_relevancy": er.context_relevancy,
                "created_at": er.created_at.isoformat() if er.created_at else None,
            }
            for er, tc in rows
        ]

    # ── 内部实现 ────────────────────────────────────────────────────────────

    async def _load_cases(
        self, kb_id: uuid.UUID, case_ids: list[uuid.UUID] | None
    ) -> list[TestCase]:
        stmt = select(TestCase).where(TestCase.kb_id == kb_id).order_by(
            TestCase.created_at.asc()
        )
        if case_ids:
            stmt = stmt.where(TestCase.id.in_(case_ids))
        rows = (await self.db.scalars(stmt)).all()
        return list(rows)

    @staticmethod
    def _infer_dataset_version(cases: list[TestCase]) -> str:
        """未显式指定时，取用例中出现最多的 dataset_version。"""
        counts: dict[str, int] = {}
        for tc in cases:
            v = tc.dataset_version or DEFAULT_DATASET_VERSION
            counts[v] = counts.get(v, 0) + 1
        if not counts:
            return DEFAULT_DATASET_VERSION
        return max(counts.items(), key=lambda kv: kv[1])[0]

    async def _config_snapshot(
        self,
        retrieval_strategy: str,
        strategy: RetrievalStrategy | None = None,
        dynamic_router: bool = False,
    ) -> dict[str, Any]:
        """采集本次运行的配置快照（不含密钥），保证实验可复现。

        阶段十二：额外记录 Router 版本、可用策略清单与本次生效的检索参数
        （strategy 存在时以其解析结果为准）。
        """
        runtime: dict[str, Any] = {}
        try:
            from src.application.model_config_service import get_effective_config_cached

            cfg = await get_effective_config_cached(self.db)
            runtime = {k: cfg.get(k) for k in _RUNTIME_CONFIG_KEYS}
        except Exception as exc:  # noqa: BLE001 — 配置读取失败不应中断评估
            logger.warning(f"运行配置快照采集失败: {exc}")

        retrieval = resolve_retrieval_config(strategy)
        return {
            "retrieval_strategy": retrieval_strategy,
            "runtime": runtime,
            # 旧结构保留（全局配置），便于与阶段九/十历史快照对比
            "retrieval": {
                "recall_top_k": settings.RECALL_TOP_K,
                "rerank_top_n": settings.RERANK_TOP_N,
                "rrf_k": settings.RRF_K,
                "relevance_threshold": settings.RELEVANCE_THRESHOLD,
                "hyde_enabled": settings.HYDE_ENABLED,
            },
            # 阶段十二：本次实际生效的策略参数（strategy=None 时即 Baseline 全局值）
            "strategy": retrieval,
            "router": {
                "dynamic_router": dynamic_router,
                "router_version": ROUTER_VERSION,
                "strategies": strategy_names(),
            },
            "evaluation": {"accuracy_threshold": settings.EVAL_ACCURACY_THRESHOLD},
        }

    async def _evaluate_case(
        self,
        kb_id: uuid.UUID,
        tc: TestCase,
        strategy: RetrievalStrategy | None = None,
        dynamic_router: bool = False,
    ) -> CaseResult:
        """单条用例评估：RAG 问答 + 指标计算。失败不抛出，记录 error 并产出 0 分。

        TASK-009：标准答案缺失或待人工确认时，answer_correctness 记为 None，
        不参与均值（AGENTS.md：不得把未标注当成错误答案）。

        阶段十二：
        - strategy 非 None → 按该策略参数检索（固定策略对照实验）
        - dynamic_router=True → 逐条 Analyzer + Router 决定策略并记入结果
        """
        base = CaseResult(
            test_case_id=str(tc.id),
            question=tc.question,
            golden_answer=tc.golden_answer,
            golden_contexts=tc.golden_contexts or [],
            question_type=tc.question_type,
            dataset_version=tc.dataset_version,
            needs_review=tc.needs_review,
        )
        try:
            use_strategy = strategy
            resource_types: list[str] | None = None
            analysis = None
            if dynamic_router:
                # 逐条 Analyzer → Router（两者均带兜底，失败不影响整批评估）
                analysis, decision = self.rag.plan_retrieval(tc.question)
                use_strategy = get_strategy(decision.strategy_name)
                resource_types = decision.resource_filter.get("resource_types") or []
                base.retrieval_strategy = decision.strategy_name
                logger.info(
                    f"动态路由 type={analysis.question_type} -> "
                    f"strategy={decision.strategy_name} q={tc.question[:30]}"
                )
            elif strategy is not None and strategy.kg_enabled:
                # 阶段十三：KG 策略以 QueryAnalysis 为图遍历输入（此处显式分析，
                # 保证"固定策略运行"与"动态路由运行"的 KG 输入口径一致）
                analysis = self.rag.analyze_query(tc.question)
            answer, hits = await self.rag.retrieve_and_answer(
                [kb_id],
                tc.question,
                strategy=use_strategy,
                resource_types=resource_types,
                analysis=analysis,
            )
            contexts = [h.get("content", "") for h in hits]
            cr = await self.metrics.context_relevancy(tc.question, contexts)
            base.answer = answer
            base.retrieved_contexts = contexts
            base.context_relevancy = round(cr, 4)
            if tc.golden_answer.strip() and not tc.needs_review:
                ac = await self.metrics.answer_correctness(
                    tc.question, answer, tc.golden_answer
                )
                base.answer_correctness = round(ac, 4)
            else:
                logger.info(
                    f"用例跳过 answer_correctness（标准答案待人工确认）: "
                    f"type={tc.question_type} q={tc.question[:30]}"
                )
        except Exception as exc:  # noqa: BLE001 — 单条失败不应中断整批评估
            logger.error(f"评估用例失败 q={tc.question[:30]}: {exc}")
            base.error = str(exc)[:200]
        return base


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def aggregate(results: list[CaseResult]) -> tuple[float, float, bool]:
    """聚合：均值上下文相关度、均值答案正确度、是否通过质量门禁。

    向后兼容旧签名（TASK-008 及之前）：answer_correctness 取「已评估」用例的均值；
    全部未评估时返回 0.0 且 passed=False（不因无数据误判为通过）。
    """
    cr, ac, _evaluated, _skipped, passed = summarize(results)
    return cr, (ac if ac is not None else 0.0), passed


def summarize(
    results: list[CaseResult],
) -> tuple[float, float | None, int, int, bool]:
    """完整聚合：均值 CR、均值 AC（无已评估用例时为 None）、已评估数、跳过数、门禁。"""
    cr = _mean([r.context_relevancy for r in results])
    scored = [r.answer_correctness for r in results if r.answer_correctness is not None]
    ac = _mean(scored) if scored else None
    evaluated = len(scored)
    skipped = len(results) - evaluated
    passed = evaluated > 0 and ac is not None and ac >= settings.EVAL_ACCURACY_THRESHOLD
    return round(cr, 4), (round(ac, 4) if ac is not None else None), evaluated, skipped, passed


def breakdown_by_question_type(results: list[CaseResult]) -> list[dict]:
    """按 question_type 分组聚合，支持「不同问题类型下的策略效果」分析。"""
    buckets: dict[str, list[CaseResult]] = {}
    for r in results:
        buckets.setdefault(r.question_type or "general", []).append(r)
    out: list[dict] = []
    for qtype in QUESTION_TYPES:
        if qtype not in buckets:
            continue
        group = buckets[qtype]
        cr, ac, evaluated, skipped, _ = summarize(group)
        out.append(
            {
                "question_type": qtype,
                "question_type_label": QUESTION_TYPE_LABELS.get(qtype, qtype),
                "case_count": len(group),
                "evaluated_count": evaluated,
                "skipped_count": skipped,
                "context_relevancy": cr,
                "answer_correctness": ac,
            }
        )
    # 未知类型（历史/自定义数据）兜底排在最后
    for qtype, group in buckets.items():
        if qtype in QUESTION_TYPES:
            continue
        cr, ac, evaluated, skipped, _ = summarize(group)
        out.append(
            {
                "question_type": qtype,
                "question_type_label": QUESTION_TYPE_LABELS.get(qtype, qtype),
                "case_count": len(group),
                "evaluated_count": evaluated,
                "skipped_count": skipped,
                "context_relevancy": cr,
                "answer_correctness": ac,
            }
        )
    return out
