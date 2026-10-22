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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.dynamic_router import DYNAMIC_ROUTER_STRATEGY, ROUTER_VERSION
from src.application.eval_metrics import RAGASMetrics, get_metrics
from src.application.evidence_gate import GATE_VERSION, gate_config
from src.application.self_reflection import REFLECTION_VERSION, reflection_config
from src.application.rag_service import RagService
from src.application.retrieval_strategies import (
    RetrievalStrategy,
    get_strategy,
    resolve_retrieval_config,
    strategy_names,
    strategy_out,
)
from src.core.config import settings
from src.core.runtime_config import get_system_value
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

# BUG-025：动态路由运行中，连策略都没选出来就失败的用例的归档标签。
# 不能写成 "dynamic_router"——否则「问题类型 × 策略 × 指标」对比会把基础设施
# 故障算成该策略的效果（AGENTS.md：禁止伪造评测结果）。
ROUTER_FAILED_STRATEGY = "router_failed"

# BUG-019（已知差异，仅标注不改行为）：评测走 RagService.retrieve_and_answer
# （非流式），该路径允许 Reflection retry；线上 SSE 路径 ask_stream 因 citations
# 已在生成前下发，换检索策略会导致证据与已下发引用卡片不一致，故固定为 False。
# 两侧行为均保持不变，只把差异写入 config_snapshot，使「评测结论能否迁移到线上」
# 这件事可追溯、可解释。
EVAL_ALLOW_RETRY = True
ONLINE_STREAM_ALLOW_RETRY = False

# BUG-023：列表分页默认值与上限。默认值沿用原先硬编码的 100 / 200，行为不变，
# 新增分页参数后可避免「只返回前 N 条且无法翻页」。
DEFAULT_RUN_PAGE_SIZE = 100
DEFAULT_HISTORY_PAGE_SIZE = 200
MAX_PAGE_SIZE = 500


def _page_clamp(limit: int, offset: int, default: int) -> tuple[int, int]:
    """分页参数兜底：非法值或超限时夹到安全区间（路由层另有 422 校验）。"""
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        limit_i = default
    try:
        offset_i = int(offset)
    except (TypeError, ValueError):
        offset_i = 0
    if limit_i <= 0:
        limit_i = default
    return min(limit_i, MAX_PAGE_SIZE), max(offset_i, 0)

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
    # 阶段十四：Evidence Gate 归档（Gate 关闭时为 None）
    gate_decision: str | None = None
    gate_version: str | None = None
    # Gate 判定 retry 时实际重试使用的策略（未重试为 None）
    retry_strategy: str | None = None
    # 阶段十五：Self Reflection 归档（Reflection 关闭时为 None）
    reflection_decision: str | None = None
    reflection_version: str | None = None
    reflection_retry_strategy: str | None = None
    reflection_reason: str | None = None


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
        use_evidence_gate: bool | None = None,
        use_self_reflection: bool | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[EvaluationRun, list[CaseResult]]:
        """运行评估的对外入口：负责开关治理与幂等保护，实际执行见 `_execute`。

        BUG-022：Gate / Reflection 开关是**就地改写** `RagService` 实例状态实现的，
        RagService 一旦被复用（共享实例），上一次实验的开关会污染下一次。
        这里在 `try/finally` 中设置与还原，任何异常路径都不残留状态。

        BUG-023：`idempotency_key` 非空时，重复提交直接返回既有 run 与其结果
        （并发重复提交由 `idempotency_key` 唯一索引在数据库层兜住），
        不再产生重复的 run + evaluation_results。
        """
        if idempotency_key:
            existing = await self._find_run_by_idempotency_key(idempotency_key)
            if existing is not None:
                if existing.kb_id != kb_id:
                    raise AppException(
                        409, "idempotency_key 已绑定其他知识库的评估运行"
                    )
                logger.info(
                    f"幂等键命中，返回既有运行 key={idempotency_key} "
                    f"run_id={existing.id}"
                )
                return existing, await self._load_results(existing.id)

        reflection_enabled = (
            bool(getattr(settings, "SELF_REFLECTION_ENABLED", True))
            if use_self_reflection is None
            else bool(use_self_reflection)
        )
        gate_enabled = (
            bool(getattr(settings, "EVIDENCE_GATE_ENABLED", True))
            if use_evidence_gate is None
            else bool(use_evidence_gate)
        )
        # BUG-022：记录原始值 → 覆盖 → finally 还原（不再依赖 try/except AttributeError）
        restore: list[tuple[Any, bool]] = []
        reflector = getattr(self.rag, "reflector", None)
        if reflector is not None and hasattr(reflector, "enabled"):
            restore.append((reflector, bool(reflector.enabled)))
            reflector.enabled = reflection_enabled
        else:  # 注入的 rag 未提供 Reflection（如测试桩）
            logger.warning("RagService 未提供 Self Reflection，本次运行不记录反思决策")
            reflection_enabled = False
        gate = getattr(self.rag, "gate", None)
        if gate is not None and hasattr(gate, "enabled"):
            restore.append((gate, bool(gate.enabled)))
            gate.enabled = gate_enabled
        else:  # 注入的 rag 未提供 Gate（如测试桩）
            logger.warning("RagService 未提供 Evidence Gate，本次运行不记录 Gate 决策")
            gate_enabled = False

        try:
            return await self._execute(
                kb_id=kb_id,
                case_ids=case_ids,
                experiment_name=experiment_name,
                retrieval_strategy=retrieval_strategy,
                dataset_version=dataset_version,
                use_dynamic_router=use_dynamic_router,
                gate_enabled=gate_enabled,
                reflection_enabled=reflection_enabled,
                idempotency_key=idempotency_key or None,
            )
        finally:
            for target, original in restore:
                target.enabled = original

    async def _execute(
        self,
        *,
        kb_id: uuid.UUID,
        case_ids: list[uuid.UUID] | None = None,
        experiment_name: str = DEFAULT_EXPERIMENT_NAME,
        retrieval_strategy: str = BASELINE_RETRIEVAL_STRATEGY,
        dataset_version: str | None = None,
        use_dynamic_router: bool = False,
        gate_enabled: bool = True,
        reflection_enabled: bool = True,
        idempotency_key: str | None = None,
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

        阶段十四：
        - use_evidence_gate=True/False 显式开关 Evidence Gate（None = 沿用全局
          settings.EVIDENCE_GATE_ENABLED），用于「Gate 开 / 关」对照实验；
        - 每条用例的 gate_decision / gate_version / retry_strategy 写入
          evaluation_results 的新增可空列；
        - Gate 配置与本次运行的决策分布写入 config_snapshot["evidence_gate"]。
          本阶段只建立 Gate 评测能力，不产出 Gate 效果结论。

        阶段十五：
        - use_self_reflection=True/False 显式开关 Self Reflection（None = 沿用全局
          settings.SELF_REFLECTION_ENABLED），用于「Reflection ON / OFF」对照实验；
        - 每条用例的 reflection_decision / reflection_version /
          reflection_retry_strategy / reflection_reason 写入 evaluation_results；
        - Reflection 配置与决策分布写入 config_snapshot["self_reflection"]。
          本阶段只建立 Reflection 评测能力，不产出任何 Reflection 效果结论。
        """
        # Gate / Reflection 开关由 run() 统一设置并在 finally 中还原（BUG-022）
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
            strategy_label,
            strategy=fixed_strategy,
            dynamic_router=use_dynamic_router,
            gate_enabled=gate_enabled,
            reflection_enabled=reflection_enabled,
        )

        run = EvaluationRun(
            kb_id=kb_id,
            experiment_name=experiment_name or DEFAULT_EXPERIMENT_NAME,
            retrieval_strategy=strategy_label,
            dataset_version=version,
            config_snapshot=config_snapshot,
            idempotency_key=idempotency_key,
        )
        self.db.add(run)
        try:
            await self.db.flush()  # 取 run.id 供 evaluation_results.run_id 引用
        except IntegrityError:
            # BUG-023：并发重复提交（另一个事务抢先落了同一幂等键）→ 回滚并返回既有运行
            await self.db.rollback()
            existing = await self._find_run_by_idempotency_key(idempotency_key or "")
            if existing is not None:
                logger.info(f"并发幂等去重 run_id={existing.id} key={idempotency_key}")
                return existing, await self._load_results(existing.id)
            raise AppException(409, "并发提交冲突，请携带唯一的 idempotency_key 重试")

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
                    # 阶段十二：动态路由运行记录逐条实际策略；否则记录 run 级策略。
                    # BUG-025：动态路由未能选出策略就失败的用例归档为 ROUTER_FAILED_STRATEGY，
                    # 绝不回落成 run 级的 "dynamic_router"（否则把故障算成策略效果）。
                    retrieval_strategy=(
                        res.retrieval_strategy
                        or (
                            ROUTER_FAILED_STRATEGY
                            if use_dynamic_router
                            else run.retrieval_strategy
                        )
                    ),
                    dataset_version=tc.dataset_version,
                    question_type=tc.question_type,
                    answer=res.answer,
                    error=res.error,
                    # 阶段十四：Gate 决策归档（Gate 关闭时为 None）
                    gate_decision=res.gate_decision,
                    gate_version=res.gate_version,
                    retry_strategy=res.retry_strategy,
                    # 阶段十五：Reflection 归档（Reflection 关闭时为 None）
                    reflection_decision=res.reflection_decision,
                    reflection_version=res.reflection_version,
                    reflection_retry_strategy=res.reflection_retry_strategy,
                    reflection_reason=res.reflection_reason,
                )
            )

        summary = summarize_full(results)
        # 阶段十四：把本次运行的 Gate 配置与决策分布并入配置快照（可复现 + 可对比）
        gate_counts: dict[str, int] = {}
        for r in results:
            if r.gate_decision:
                gate_counts[r.gate_decision] = gate_counts.get(r.gate_decision, 0) + 1
        # 阶段十五：Reflection 决策分布（同样并入快照）
        reflection_counts: dict[str, int] = {}
        for r in results:
            if r.reflection_decision:
                reflection_counts[r.reflection_decision] = (
                    reflection_counts.get(r.reflection_decision, 0) + 1
                )
        # BUG-025：动态路由「选策略即失败」的用例数，便于把故障与策略效果分开看
        router_failed = sum(
            1
            for r in results
            if use_dynamic_router and r.error is not None and not r.retrieval_strategy
        )
        run.config_snapshot = {
            **(config_snapshot or {}),
            "router": {
                **(config_snapshot or {}).get("router", {}),
                "failed_count": router_failed,
            },
            "evidence_gate": {
                **gate_config(gate_enabled),
                "decision_counts": gate_counts,
            },
            "self_reflection": {
                **reflection_config(reflection_enabled),
                "decision_counts": reflection_counts,
                # BUG-019：仅标注两侧差异，不改变任何运行时行为
                "allow_retry": bool(reflection_enabled and EVAL_ALLOW_RETRY),
                "allow_retry_online_stream": ONLINE_STREAM_ALLOW_RETRY,
            },
        }
        run.case_count = len(results)
        run.evaluated_count = summary.evaluated_count
        run.skipped_count = summary.skipped_count
        # BUG-011：失败用例单独计数，且已从上文所有均值中剔除
        run.failed_count = summary.failed_count
        run.context_relevancy = summary.context_relevancy
        run.answer_correctness = summary.answer_correctness
        run.passed = summary.passed
        run.threshold = settings.EVAL_ACCURACY_THRESHOLD
        run.by_question_type = breakdown_by_question_type(results)
        await self.db.commit()
        logger.info(
            f"评估完成 run_id={run.id} experiment={run.experiment_name} "
            f"strategy={run.retrieval_strategy} dataset={run.dataset_version} "
            f"gate={'on' if gate_enabled else 'off'}:{GATE_VERSION} "
            f"reflection={'on' if reflection_enabled else 'off'}:{REFLECTION_VERSION} "
            f"kb={kb_id} cases={len(results)} evaluated={summary.evaluated_count} "
            f"skipped={summary.skipped_count} failed={summary.failed_count} "
            f"cr={summary.context_relevancy:.3f} "
            f"ac={'None' if summary.answer_correctness is None else f'{summary.answer_correctness:.3f}'}"
        )
        if summary.failed_count:
            # 失败通常代表基础设施故障（Milvus / LLM / Embedding 不可用），必须显性暴露，
            # 不能混进"检索质量为 0"的报告里（AGENTS.md：禁止伪造评测结果）
            logger.error(
                f"评估存在失败用例 run_id={run.id} "
                f"failed={summary.failed_count}/{len(results)}："
                f"这些用例未计入任何均值，请检查基础设施可用性"
            )
        return run, results

    # ── BUG-023：幂等查询入口 ───────────────────────────────────────────────

    async def _find_run_by_idempotency_key(
        self, key: str
    ) -> EvaluationRun | None:
        """按幂等键查找既有运行（NULL 键不参与）。"""
        if not key:
            return None
        stmt = select(EvaluationRun).where(EvaluationRun.idempotency_key == key)
        return (await self.db.scalars(stmt)).first()

    async def _load_results(self, run_id: uuid.UUID) -> list[CaseResult]:
        """从 evaluation_results 重建 per-case 结果（幂等/并发去重后复用既有运行）。"""
        rows = (
            await self.db.execute(
                select(EvaluationResult, TestCase)
                .join(TestCase, TestCase.id == EvaluationResult.test_case_id)
                .where(EvaluationResult.run_id == str(run_id))
                .order_by(EvaluationResult.created_at.asc())
            )
        ).all()
        return [
            CaseResult(
                test_case_id=str(er.test_case_id),
                question=tc.question,
                golden_answer=tc.golden_answer,
                golden_contexts=tc.golden_contexts or [],
                answer=er.answer or "",
                retrieved_contexts=er.retrieved_contexts or [],
                context_relevancy=float(er.context_relevancy or 0.0),
                answer_correctness=er.answer_correctness,
                error=er.error,
                question_type=er.question_type or "general",
                dataset_version=er.dataset_version or DEFAULT_DATASET_VERSION,
                needs_review=tc.needs_review,
                retrieval_strategy=er.retrieval_strategy,
                gate_decision=er.gate_decision,
                gate_version=er.gate_version,
                retry_strategy=er.retry_strategy,
                reflection_decision=er.reflection_decision,
                reflection_version=er.reflection_version,
                reflection_retry_strategy=er.reflection_retry_strategy,
                reflection_reason=er.reflection_reason,
            )
            for er, tc in rows
        ]

    @staticmethod
    def list_strategies() -> list[dict]:
        """阶段十二：可用检索策略清单（含生效参数）。

        供 Experiment A（Baseline）与 Experiment B（固定策略 / 动态路由）对比时选择，
        也便于确认不同策略的参数确实存在差异。
        """
        return [strategy_out(name) for name in strategy_names()]

    async def list_runs(
        self,
        kb_id: uuid.UUID | None = None,
        accessible_kb_ids: set[uuid.UUID] | None = None,
        limit: int = DEFAULT_RUN_PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict]:
        """实验运行归档列表（按时间倒序），用于 Baseline 与消融实验对比。

        accessible_kb_ids：非 None 时只返回这些知识库下的运行（权限隔离，
        BUG-001）。默认 None 表示不过滤，保持旧调用行为兼容。

        BUG-023：默认值与旧行为一致（limit=100、offset=0），新增分页参数，
        上限由 `_page_clamp` 兜住，避免一次性拉取全表。
        """
        limit, offset = _page_clamp(limit, offset, DEFAULT_RUN_PAGE_SIZE)
        stmt = (
            select(EvaluationRun)
            .order_by(EvaluationRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if kb_id is not None:
            stmt = stmt.where(EvaluationRun.kb_id == kb_id)
        elif accessible_kb_ids is not None:
            stmt = stmt.where(EvaluationRun.kb_id.in_(accessible_kb_ids))
        rows = (await self.db.scalars(stmt)).all()
        return [self._run_out(r) for r in rows]

    async def get_run(self, run_id: uuid.UUID) -> dict:
        run = await self.db.get(EvaluationRun, run_id)
        if run is None:
            raise NotFoundError("评估运行不存在")
        return self._run_out(run)

    @staticmethod
    def _run_out(run: EvaluationRun) -> dict:
        # getattr 兜底：迁移尚未执行的历史库/对象无该属性时按 0 处理
        failed = int(getattr(run, "failed_count", 0) or 0)
        return {
            "run_id": str(run.id),
            "kb_id": str(run.kb_id),
            "experiment_name": run.experiment_name,
            "retrieval_strategy": run.retrieval_strategy,
            "dataset_version": run.dataset_version,
            "case_count": run.case_count,
            "evaluated_count": run.evaluated_count,
            "skipped_count": run.skipped_count,
            # BUG-011：失败用例数 + 整轮是否因基础设施故障不可信
            "failed_count": failed,
            "infra_failed": infra_failed(failed, run.case_count),
            "context_relevancy": run.context_relevancy,
            "answer_correctness": run.answer_correctness,
            "passed": run.passed,
            "threshold": run.threshold,
            "config_snapshot": run.config_snapshot or {},
            "by_question_type": run.by_question_type or [],
            "created_at": run.created_at.isoformat() if run.created_at else None,
        }

    async def list_history(
        self,
        kb_id: uuid.UUID | None = None,
        run_id: str | None = None,
        accessible_kb_ids: set[uuid.UUID] | None = None,
        limit: int = DEFAULT_HISTORY_PAGE_SIZE,
        offset: int = 0,
    ) -> list[dict]:
        """历史评估结果（关联 test_case 取问题与标准答案）。

        accessible_kb_ids：非 None 时只返回这些知识库下的用例结果（权限隔离，
        BUG-001）。默认 None 表示不过滤，保持旧调用行为兼容。

        BUG-023：默认值与旧行为一致（limit=200、offset=0），新增分页参数，
        上限由 `_page_clamp` 兜住。
        """
        limit, offset = _page_clamp(limit, offset, DEFAULT_HISTORY_PAGE_SIZE)
        stmt = (
            select(EvaluationResult, TestCase)
            .join(TestCase, TestCase.id == EvaluationResult.test_case_id)
            .order_by(EvaluationResult.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if kb_id is not None:
            stmt = stmt.where(TestCase.kb_id == kb_id)
        elif accessible_kb_ids is not None:
            stmt = stmt.where(TestCase.kb_id.in_(accessible_kb_ids))
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
                # 阶段十四：Evidence Gate 归档（Gate 关闭时为 None）
                "gate_decision": er.gate_decision,
                "gate_version": er.gate_version,
                "retry_strategy": er.retry_strategy,
                # 阶段十五：Self Reflection 归档（Reflection 关闭时为 None）
                "reflection_decision": er.reflection_decision,
                "reflection_version": er.reflection_version,
                "reflection_retry_strategy": er.reflection_retry_strategy,
                "reflection_reason": er.reflection_reason,
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
        gate_enabled: bool = True,
        reflection_enabled: bool = True,
    ) -> dict[str, Any]:
        """采集本次运行的配置快照（不含密钥），保证实验可复现。

        阶段十二：额外记录 Router 版本、可用策略清单与本次生效的检索参数
        （strategy 存在时以其解析结果为准）。

        阶段十四：记录 Evidence Gate 版本 / 开关 / 阈值（决策分布由 run() 在
        用例跑完后补写，因为需要实际结果）。

        阶段十五：记录 Self Reflection 版本 / 开关 / LLM 开关 / 上限
        （决策分布同样由 run() 补写）。
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
                # 记录本次真正生效的值（后台配置可覆盖 .env）
                "recall_top_k": int(get_system_value("recall_top_k")),
                "rerank_top_n": int(get_system_value("rerank_top_n")),
                "rrf_k": settings.RRF_K,
                "relevance_threshold": float(get_system_value("relevance_threshold")),
                "hyde_enabled": settings.HYDE_ENABLED,
            },
            # 阶段十二：本次实际生效的策略参数（strategy=None 时即 Baseline 全局值）
            "strategy": retrieval,
            "router": {
                "dynamic_router": dynamic_router,
                "router_version": ROUTER_VERSION,
                "strategies": strategy_names(),
            },
            # 阶段十四：Evidence Gate 配置（version / enabled / 阈值 / retry 映射）
            "evidence_gate": gate_config(gate_enabled),
            # 阶段十五：Self Reflection 配置（version / enabled / llm_enabled / 上限）
            "self_reflection": reflection_config(reflection_enabled),
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

        阶段十四：读取本次检索的 Evidence Gate 决策（RagService.last_gate_decision），
        归档 gate_decision / gate_version / retry_strategy（Gate 关闭时为 None）。

        阶段十五：读取本次生成的 Self Reflection 决策
        （RagService.last_reflection_decision），归档 reflection_decision /
        reflection_version / reflection_retry_strategy / reflection_reason
        （Reflection 关闭时为 None）。

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
            decision = None  # 阶段十四：动态路由时供 Evidence Gate 选择 retry 策略
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
                router_decision=decision if dynamic_router else None,
            )
            # 阶段十四：归档本次检索的 Gate 决策（Gate 关闭时为 None）
            gate_decision = getattr(self.rag, "last_gate_decision", None)
            if gate_decision is not None:
                base.gate_decision = gate_decision.decision
                base.gate_version = gate_decision.gate_version
                base.retry_strategy = (
                    gate_decision.retry_strategy if gate_decision.retried else None
                )
                logger.info(
                    f"Evidence Gate type={tc.question_type} "
                    f"decision={gate_decision.decision} reason={gate_decision.reason} "
                    f"q={tc.question[:30]}"
                )
            # 阶段十五：归档本次生成的 Self Reflection 决策
            reflection_decision = getattr(self.rag, "last_reflection_decision", None)
            if reflection_decision is not None:
                base.reflection_decision = reflection_decision.decision
                base.reflection_version = reflection_decision.reflection_version
                base.reflection_retry_strategy = (
                    reflection_decision.retry_strategy
                    if reflection_decision.retried
                    else None
                )
                base.reflection_reason = reflection_decision.reason[:255] or None
                logger.info(
                    f"Self Reflection type={tc.question_type} "
                    f"decision={reflection_decision.decision} "
                    f"issues={reflection_decision.issues} "
                    f"retry={reflection_decision.reflection_retry_count} "
                    f"revised={reflection_decision.revised} q={tc.question[:30]}"
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


@dataclass
class RunSummary:
    """一次评估运行的聚合结果。

    BUG-011：三类计数互斥且覆盖全部用例，语义不再混淆——
    - evaluated_count：实际计算了 answer_correctness 的用例
    - skipped_count：标准答案缺失/待人工确认，未计算 answer_correctness
    - failed_count：检索/生成抛异常，本轮没有拿到任何结果（error 非空）

    failed_count **不参与任何均值**：基础设施故障（Milvus 宕机、LLM 不可用）
    不等于「检索质量为 0」，混进均值会直接污染实验结论。
    """

    context_relevancy: float
    answer_correctness: float | None
    evaluated_count: int
    skipped_count: int
    failed_count: int
    passed: bool

    @property
    def total_count(self) -> int:
        return self.evaluated_count + self.skipped_count + self.failed_count

    @property
    def infra_failed(self) -> bool:
        """整轮运行是否因基础设施故障而不可信（所有用例都失败了）。"""
        return infra_failed(self.failed_count, self.total_count)


def infra_failed(failed_count: int, case_count: int) -> bool:
    """BUG-011：全部用例失败 ⇒ 本报告不可信，调用方应显式提示而非展示一份 0 分报告。"""
    failed = int(failed_count or 0)
    total = int(case_count or 0)
    return failed > 0 and total > 0 and failed >= total


def summarize_full(results: list[CaseResult]) -> RunSummary:
    """完整聚合（含失败维度）：失败用例不计入 CR / AC 均值，只计入 failed_count。"""
    ok = [r for r in results if r.error is None]
    failed_count = len(results) - len(ok)
    cr = _mean([r.context_relevancy for r in ok])
    scored = [r.answer_correctness for r in ok if r.answer_correctness is not None]
    ac = _mean(scored) if scored else None
    evaluated = len(scored)
    skipped = len(ok) - evaluated
    passed = evaluated > 0 and ac is not None and ac >= settings.EVAL_ACCURACY_THRESHOLD
    return RunSummary(
        context_relevancy=round(cr, 4),
        answer_correctness=round(ac, 4) if ac is not None else None,
        evaluated_count=evaluated,
        skipped_count=skipped,
        failed_count=failed_count,
        passed=passed,
    )


def summarize(
    results: list[CaseResult],
) -> tuple[float, float | None, int, int, bool]:
    """旧签名聚合（均值语义随 BUG-011 修正：失败用例不进均值）。

    保留五元组形式兼容 TASK-008 及之前的调用方；需要失败维度请用 `summarize_full`。
    """
    s = summarize_full(results)
    return (
        s.context_relevancy,
        s.answer_correctness,
        s.evaluated_count,
        s.skipped_count,
        s.passed,
    )


def _bucket_out(qtype: str, group: list[CaseResult]) -> dict:
    """单个 question_type 分组的指标（含失败计数）。"""
    s = summarize_full(group)
    return {
        "question_type": qtype,
        "question_type_label": QUESTION_TYPE_LABELS.get(qtype, qtype),
        "case_count": len(group),
        "evaluated_count": s.evaluated_count,
        "skipped_count": s.skipped_count,
        "failed_count": s.failed_count,
        "context_relevancy": s.context_relevancy,
        "answer_correctness": s.answer_correctness,
    }


def breakdown_by_question_type(results: list[CaseResult]) -> list[dict]:
    """按 question_type 分组聚合，支持「不同问题类型下的策略效果」分析。"""
    buckets: dict[str, list[CaseResult]] = {}
    for r in results:
        buckets.setdefault(r.question_type or "general", []).append(r)
    ordered = [_bucket_out(q, buckets[q]) for q in QUESTION_TYPES if q in buckets]
    # 未知类型（历史/自定义数据）兜底排在最后
    extra = [_bucket_out(q, g) for q, g in buckets.items() if q not in QUESTION_TYPES]
    return ordered + extra
