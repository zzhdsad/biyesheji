"""评估路由：上传测试集、运行 RAGAS 评估、查看报告与历史。

TECH_DESIGN / PRD：内置自动化评估工具，上传测试集 JSON（question/golden_answer/
golden_contexts）后批量运行 RAG 流程，计算 Context Relevancy 与 Answer Correctness
（中文适配 RAGAS 指标），生成可视化报告；answer_correctness ≥ 75% 视为通过质量门禁。
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.evaluation_service import CaseResult, EvaluationService, aggregate
from src.core.config import settings
from src.infrastructure.database import get_db

router = APIRouter(prefix="/evaluation", tags=["evaluation"])


class TestCaseItem(BaseModel):
    question: str = Field(min_length=1)
    golden_answer: str = Field(default="")
    golden_contexts: list[str] = Field(default_factory=list)


class EvaluationUploadRequest(BaseModel):
    """上传测试集：绑定目标知识库（检索范围与权限依据）。"""

    kb_id: uuid.UUID
    cases: list[TestCaseItem] = Field(min_length=1)


class EvaluationRunRequest(BaseModel):
    """运行评估：基于已上传测试集；可选 case_ids 子集。"""

    kb_id: uuid.UUID
    case_ids: list[uuid.UUID] | None = None


class CaseResultOut(BaseModel):
    test_case_id: str
    question: str
    golden_answer: str
    golden_contexts: list[str]
    answer: str
    retrieved_contexts: list[str]
    context_relevancy: float
    answer_correctness: float
    error: str | None = None


class EvaluationReport(BaseModel):
    """评估报告：聚合指标 + 逐条明细，供前端可视化。"""

    run_id: str
    case_count: int
    context_relevancy: float
    answer_correctness: float
    # 质量门禁：AGENTS.md 要求准确率 ≥ 75% 才允许合入
    passed: bool
    threshold: float
    results: list[CaseResultOut]


class EvaluationHistoryItem(BaseModel):
    id: str
    question: str
    golden_answer: str
    answer_correctness: float
    context_relevancy: float
    created_at: str | None


def _to_case_out(r: CaseResult) -> CaseResultOut:
    return CaseResultOut(
        test_case_id=r.test_case_id,
        question=r.question,
        golden_answer=r.golden_answer,
        golden_contexts=r.golden_contexts,
        answer=r.answer,
        retrieved_contexts=r.retrieved_contexts,
        context_relevancy=r.context_relevancy,
        answer_correctness=r.answer_correctness,
        error=r.error,
    )


@router.post("/upload")
async def upload_test_set(
    payload: EvaluationUploadRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    """上传测试集（问题-标准答案-上下文），写入 test_cases 表。

    AGENTS.md 建议测试集 ≥30 条；本端点不强制，由调用方保证质量。
    """
    service = EvaluationService(db)
    ids = await service.save_test_set(
        payload.kb_id,
        [c.model_dump() for c in payload.cases],
    )
    return {"kb_id": str(payload.kb_id), "uploaded": len(ids), "case_ids": [str(i) for i in ids]}


@router.post("/run", response_model=EvaluationReport)
async def run_evaluation(
    payload: EvaluationRunRequest, db: AsyncSession = Depends(get_db)
) -> EvaluationReport:
    """运行 RAGAS 评估：批量 RAG 问答 → 指标计算 → 入库 → 聚合报告。"""
    service = EvaluationService(db)
    run_id, results = await service.run(payload.kb_id, payload.case_ids)
    cr, ac, passed = aggregate(results)
    return EvaluationReport(
        run_id=run_id,
        case_count=len(results),
        context_relevancy=cr,
        answer_correctness=ac,
        passed=passed,
        threshold=settings.EVAL_ACCURACY_THRESHOLD,
        results=[_to_case_out(r) for r in results],
    )


@router.get("/results", response_model=list[EvaluationHistoryItem])
async def list_results(
    kb_id: uuid.UUID | None = None, db: AsyncSession = Depends(get_db)
) -> list[dict]:
    """历史评估结果（关联 test_case 展示问题与标准答案，按时间倒序）。"""
    service = EvaluationService(db)
    return await service.list_history(kb_id)
