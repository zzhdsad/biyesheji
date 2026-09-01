"""评估编排服务：批量执行 RAG → RAGAS 指标计算 → 持久化 → 聚合报告。

TECH_DESIGN / AGENTS.md：
- 内置自动化评估工具，支持上传测试集（≥30 条），自动计算上下文相关度与答案正确率。
- 质量门禁：answer_correctness（准确率）≥ 75% 才允许合入 develop。

复用 RagService.retrieve_and_answer（不持久化，避免评估污染聊天记录与缓存），
与 RAGASMetrics（中文适配指标）。
"""

import uuid
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.eval_metrics import RAGASMetrics, get_metrics
from src.application.rag_service import RagService
from src.core.config import settings
from src.core.exceptions import AppException
from src.domain.models import EvaluationResult, TestCase


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
    answer_correctness: float = 0.0
    error: str | None = None  # 该用例评估失败时的原因（仍产出 0 分）


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

    async def save_test_set(
        self, kb_id: uuid.UUID, cases: list[dict]
    ) -> list[uuid.UUID]:
        """持久化测试集到 test_cases 表。返回用例 ID 列表。"""
        if not cases:
            raise AppException(422, "测试集不能为空")
        ids: list[uuid.UUID] = []
        for c in cases:
            # 显式生成主键，避免依赖 flush 后回填（部分 async 配置下属性未即时刷新）
            tc_id = uuid.uuid4()
            self.db.add(
                TestCase(
                    id=tc_id,
                    kb_id=kb_id,
                    question=c["question"],
                    golden_answer=c["golden_answer"],
                    golden_contexts=c.get("golden_contexts", []),
                )
            )
            ids.append(tc_id)
        await self.db.commit()
        logger.info(f"测试集已写入 kb={kb_id} count={len(ids)}")
        return ids

    async def run(
        self,
        kb_id: uuid.UUID,
        case_ids: list[uuid.UUID] | None = None,
    ) -> tuple[str, list[CaseResult]]:
        """运行评估。返回 (run_id, per-case results)。

        - 从 test_cases 表加载用例（按 kb_id；可选 case_ids 过滤）
        - 逐条调用 RAG 检索+生成，计算 RAGAS 指标
        - 写入 evaluation_results 表
        - 聚合均值由路由层基于 results 计算
        """
        cases = await self._load_cases(kb_id, case_ids)
        if not cases:
            raise AppException(404, "未找到测试用例：请先上传测试集")

        run_id = str(uuid.uuid4())
        results: list[CaseResult] = []
        for tc in cases:
            res = await self._evaluate_case(kb_id, tc)
            results.append(res)
            # 持久化评估结果（失败用例也留存 0 分，便于审计）
            self.db.add(
                EvaluationResult(
                    test_case_id=tc.id,
                    retrieved_contexts=res.retrieved_contexts,
                    context_relevancy=res.context_relevancy,
                    answer_correctness=res.answer_correctness,
                )
            )
        await self.db.commit()
        logger.info(
            f"评估完成 run_id={run_id} kb={kb_id} cases={len(results)} "
            f"cr={_mean([r.context_relevancy for r in results]):.3f} "
            f"ac={_mean([r.answer_correctness for r in results]):.3f}"
        )
        return run_id, results

    async def list_history(self, kb_id: uuid.UUID | None = None) -> list[dict]:
        """历史评估结果（关联 test_case 取问题与标准答案）。"""
        stmt = (
            select(EvaluationResult, TestCase)
            .join(TestCase, TestCase.id == EvaluationResult.test_case_id)
            .order_by(EvaluationResult.created_at.desc())
            .limit(200)
        )
        if kb_id is not None:
            stmt = stmt.where(TestCase.kb_id == kb_id)
        rows = (await self.db.execute(stmt)).all()
        return [
            {
                "id": str(er.id),
                "question": tc.question,
                "golden_answer": tc.golden_answer,
                "answer_correctness": er.answer_correctness,
                "context_relevancy": er.context_relevancy,
                "created_at": er.created_at.isoformat() if er.created_at else None,
            }
            for er, tc in rows
        ]

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

    async def _evaluate_case(self, kb_id: uuid.UUID, tc: TestCase) -> CaseResult:
        """单条用例评估：RAG 问答 + 指标计算。失败不抛出，记录 error 并产出 0 分。"""
        base = CaseResult(
            test_case_id=str(tc.id),
            question=tc.question,
            golden_answer=tc.golden_answer,
            golden_contexts=tc.golden_contexts or [],
        )
        try:
            answer, hits = await self.rag.retrieve_and_answer([kb_id], tc.question)
            contexts = [h.get("content", "") for h in hits]
            cr = await self.metrics.context_relevancy(tc.question, contexts)
            ac = await self.metrics.answer_correctness(
                tc.question, answer, tc.golden_answer
            )
            base.answer = answer
            base.retrieved_contexts = contexts
            base.context_relevancy = round(cr, 4)
            base.answer_correctness = round(ac, 4)
        except Exception as exc:  # noqa: BLE001 — 单条失败不应中断整批评估
            logger.error(f"评估用例失败 q={tc.question[:30]}: {exc}")
            base.error = str(exc)[:200]
        return base


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def aggregate(results: list[CaseResult]) -> tuple[float, float, bool]:
    """聚合：均值上下文相关度、均值答案正确度、是否通过质量门禁。"""
    n = len(results)
    cr = _mean([r.context_relevancy for r in results])
    ac = _mean([r.answer_correctness for r in results])
    passed = n > 0 and ac >= settings.EVAL_ACCURACY_THRESHOLD
    return round(cr, 4), round(ac, 4), passed
