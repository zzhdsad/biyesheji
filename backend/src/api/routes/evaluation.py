"""评估路由：上传测试集、运行 RAGAS 评估、查看报告与历史、实验运行归档。

TECH_DESIGN / PRD：内置自动化评估工具，上传测试集 JSON（question/golden_answer/
golden_contexts）后批量运行 RAG 流程，计算 Context Relevancy 与 Answer Correctness
（中文适配 RAGAS 指标），生成可视化报告；answer_correctness ≥ 75% 视为通过质量门禁。

TASK-009 扩展（保持旧字段与旧调用兼容，新增均为可选字段/新端点）：
- 测试集：question_type / dataset_version / needs_review / source_reference
- 运行：experiment_name / retrieval_strategy / dataset_version → 归档到 evaluation_runs
- 新端点：GET /runs（实验对比）、GET /test-cases（查看测试集）、
  PATCH /test-cases/{id}（人工确认标准答案）
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.evaluation_service import (
    BASELINE_RETRIEVAL_STRATEGY,
    DEFAULT_DATASET_VERSION,
    DEFAULT_EXPERIMENT_NAME,
    QUESTION_TYPES,
    CaseResult,
    EvaluationService,
    aggregate,
)
from src.core.config import settings
from src.infrastructure.database import get_db

router = APIRouter(prefix="/evaluation", tags=["evaluation"])


class TestCaseItem(BaseModel):
    question: str = Field(min_length=1)
    golden_answer: str = Field(default="")
    golden_contexts: list[str] = Field(default_factory=list)
    # TASK-009：问题分类（受控词表见 QUESTION_TYPES），默认 general
    question_type: str = Field(default="general")
    dataset_version: str | None = None
    # 未显式给出时：提供了 golden_answer 视为已确认，否则标记需人工确认
    needs_review: bool | None = None
    source_reference: str = Field(default="")


class EvaluationUploadRequest(BaseModel):
    """上传测试集：绑定目标知识库（检索范围与权限依据）。"""

    kb_id: uuid.UUID
    cases: list[TestCaseItem] = Field(min_length=1)
    # 测试集版本（如 tcm-v1）；用例未单独指定时使用此值
    dataset_version: str | None = None


class EvaluationRunRequest(BaseModel):
    """运行评估：基于已上传测试集；可选 case_ids 子集与实验维度。"""

    kb_id: uuid.UUID
    case_ids: list[uuid.UUID] | None = None
    experiment_name: str = DEFAULT_EXPERIMENT_NAME
    retrieval_strategy: str = BASELINE_RETRIEVAL_STRATEGY
    # 留空时按用例的 dataset_version 自动推断
    dataset_version: str | None = None


class TestCaseUpdateRequest(BaseModel):
    """人工确认/修订标准答案（TASK-009 录入机制）。"""

    golden_answer: str | None = None
    golden_contexts: list[str] | None = None
    source_reference: str | None = None
    question_type: str | None = None
    needs_review: bool | None = None


class CaseResultOut(BaseModel):
    test_case_id: str
    question: str
    golden_answer: str
    golden_contexts: list[str]
    answer: str
    retrieved_contexts: list[str]
    context_relevancy: float
    # None：标准答案缺失或待人工确认，未参与 answer_correctness 计算
    answer_correctness: float | None = None
    error: str | None = None
    # TASK-009：问题分类与数据集版本
    question_type: str = "general"
    dataset_version: str = DEFAULT_DATASET_VERSION
    needs_review: bool = False


class QuestionTypeMetric(BaseModel):
    """按问题类型分组的指标（TASK-011 按问题类型分析检索策略效果）。"""

    question_type: str
    question_type_label: str
    case_count: int
    evaluated_count: int
    skipped_count: int
    context_relevancy: float
    answer_correctness: float | None = None


class EvaluationReport(BaseModel):
    """评估报告：聚合指标 + 逐条明细 + 按问题类型分组，供前端可视化。"""

    run_id: str
    kb_id: str
    case_count: int
    context_relevancy: float
    # None：本次运行没有已确认标准答案的用例（未参与门禁判定）
    answer_correctness: float | None = None
    # 质量门禁：AGENTS.md 要求准确率 ≥ 75% 才允许合入
    passed: bool
    threshold: float
    # TASK-009 实验维度（供 Baseline 与后续消融实验对比）
    experiment_name: str = DEFAULT_EXPERIMENT_NAME
    retrieval_strategy: str = BASELINE_RETRIEVAL_STRATEGY
    dataset_version: str = DEFAULT_DATASET_VERSION
    evaluated_count: int = 0
    skipped_count: int = 0
    by_question_type: list[QuestionTypeMetric] = Field(default_factory=list)
    results: list[CaseResultOut]


class EvaluationHistoryItem(BaseModel):
    id: str
    question: str
    golden_answer: str
    answer_correctness: float | None = None
    context_relevancy: float
    created_at: str | None
    # TASK-009：运行归档与问题分类（历史数据无 run_id 时为 None）
    run_id: str | None = None
    experiment_name: str | None = None
    retrieval_strategy: str | None = None
    dataset_version: str | None = None
    question_type: str | None = None


class EvaluationRunOut(BaseModel):
    """实验运行归档（Baseline / 消融实验对比用）。"""

    run_id: str
    kb_id: str
    experiment_name: str
    retrieval_strategy: str
    dataset_version: str
    case_count: int
    evaluated_count: int
    skipped_count: int
    context_relevancy: float
    answer_correctness: float | None = None
    passed: bool
    threshold: float
    config_snapshot: dict = Field(default_factory=dict)
    by_question_type: list[QuestionTypeMetric] = Field(default_factory=list)
    created_at: str | None = None


class TestCaseOut(BaseModel):
    id: str
    kb_id: str
    question: str
    golden_answer: str
    golden_contexts: list[str]
    question_type: str
    question_type_label: str
    dataset_version: str
    needs_review: bool
    source_reference: str
    created_at: str | None = None


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
        question_type=r.question_type,
        dataset_version=r.dataset_version,
        needs_review=r.needs_review,
    )


@router.get("/question-types")
async def list_question_types() -> dict:
    """问题类型受控词表（供前端下拉与人工标注使用）。"""
    from src.application.evaluation_service import QUESTION_TYPE_LABELS

    return {
        "question_types": [
            {"value": t, "label": QUESTION_TYPE_LABELS.get(t, t)} for t in QUESTION_TYPES
        ]
    }


@router.post("/upload")
async def upload_test_set(
    payload: EvaluationUploadRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    """上传测试集（问题-标准答案-上下文），写入 test_cases 表。

    AGENTS.md 建议测试集 ≥30 条；本端点不强制，由调用方保证质量。
    TASK-009：支持 question_type / dataset_version / needs_review / source_reference。
    """
    service = EvaluationService(db)
    ids = await service.save_test_set(
        payload.kb_id,
        [c.model_dump() for c in payload.cases],
        dataset_version=payload.dataset_version,
    )
    return {
        "kb_id": str(payload.kb_id),
        "uploaded": len(ids),
        "case_ids": [str(i) for i in ids],
        "dataset_version": payload.dataset_version or DEFAULT_DATASET_VERSION,
    }


@router.get("/test-cases", response_model=list[TestCaseOut])
async def list_test_cases(
    kb_id: uuid.UUID,
    dataset_version: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """查看指定知识库的测试集（含问题分类、数据集版本与人工确认状态）。"""
    service = EvaluationService(db)
    return await service.list_test_cases(kb_id, dataset_version)


@router.patch("/test-cases/{case_id}", response_model=TestCaseOut)
async def confirm_test_case(
    case_id: uuid.UUID,
    payload: TestCaseUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """人工确认/修订单条用例的标准答案。

    传入 golden_answer 后默认 needs_review=False（已确认），
    该用例在后续运行中才会计算 answer_correctness。
    """
    service = EvaluationService(db)
    return await service.confirm_test_case(
        case_id,
        golden_answer=payload.golden_answer,
        golden_contexts=payload.golden_contexts,
        source_reference=payload.source_reference,
        question_type=payload.question_type,
        needs_review=payload.needs_review,
    )


@router.post("/run", response_model=EvaluationReport)
async def run_evaluation(
    payload: EvaluationRunRequest, db: AsyncSession = Depends(get_db)
) -> EvaluationReport:
    """运行 RAGAS 评估：批量 RAG 问答 → 指标计算 → 入库 → 聚合报告 → 实验归档。"""
    service = EvaluationService(db)
    run, results = await service.run(
        payload.kb_id,
        payload.case_ids,
        experiment_name=payload.experiment_name,
        retrieval_strategy=payload.retrieval_strategy,
        dataset_version=payload.dataset_version,
    )
    cr, ac, passed = aggregate(results)
    return EvaluationReport(
        run_id=str(run.id),
        kb_id=str(run.kb_id),
        case_count=len(results),
        context_relevancy=cr,
        answer_correctness=run.answer_correctness,
        passed=passed,
        threshold=settings.EVAL_ACCURACY_THRESHOLD,
        experiment_name=run.experiment_name,
        retrieval_strategy=run.retrieval_strategy,
        dataset_version=run.dataset_version,
        evaluated_count=run.evaluated_count,
        skipped_count=run.skipped_count,
        by_question_type=[
            QuestionTypeMetric(**item) for item in (run.by_question_type or [])
        ],
        results=[_to_case_out(r) for r in results],
    )


@router.get("/runs", response_model=list[EvaluationRunOut])
async def list_runs(
    kb_id: uuid.UUID | None = None, db: AsyncSession = Depends(get_db)
) -> list[dict]:
    """实验运行归档列表（Baseline 与消融实验对比，按时间倒序）。"""
    service = EvaluationService(db)
    return await service.list_runs(kb_id)


@router.get("/runs/{run_id}", response_model=EvaluationRunOut)
async def get_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """单条实验运行归档详情（含配置快照与按问题类型指标）。"""
    service = EvaluationService(db)
    return await service.get_run(run_id)


@router.get("/results", response_model=list[EvaluationHistoryItem])
async def list_results(
    kb_id: uuid.UUID | None = None,
    run_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """历史评估结果（关联 test_case 展示问题与标准答案，按时间倒序）。"""
    service = EvaluationService(db)
    return await service.list_history(kb_id, run_id)


@router.get("/metrics/breakdown")
async def metrics_breakdown() -> dict:
    """当前评测指标说明（保持 context_relevancy / answer_correctness 两个指标）。"""
    return {
        "metrics": [
            {
                "key": "context_relevancy",
                "name": "上下文相关度",
                "definition": "检索上下文中对回答问题有实质帮助的句子占比（RAGAS 定义，中文适配）",
                "requires_golden_answer": False,
            },
            {
                "key": "answer_correctness",
                "name": "答案正确度",
                "definition": (
                    "0.5 × 事实相似度 F1（TP/FP/FN）+ 0.5 × 语义相似度余弦；"
                    "标准答案缺失或待人工确认时不计算（记为未评估）"
                ),
                "requires_golden_answer": True,
            },
        ],
        "threshold": settings.EVAL_ACCURACY_THRESHOLD,
        "baseline_strategy": BASELINE_RETRIEVAL_STRATEGY,
    }
