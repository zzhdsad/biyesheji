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

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.evaluation_service import (
    BASELINE_RETRIEVAL_STRATEGY,
    DEFAULT_DATASET_VERSION,
    DEFAULT_EXPERIMENT_NAME,
    DEFAULT_HISTORY_PAGE_SIZE,
    DEFAULT_RUN_PAGE_SIZE,
    MAX_PAGE_SIZE,
    QUESTION_TYPES,
    CaseResult,
    EvaluationService,
    aggregate,
    infra_failed,
)
from src.core.config import settings
from src.core.deps import get_accessible_kb_ids, require_kb_read, require_kb_write
from src.core.exceptions import AppException, NotFoundError
from src.domain.models import EvaluationRun, TestCase
from src.infrastructure.database import get_db

router = APIRouter(prefix="/evaluation", tags=["evaluation"])


# ── 权限校验（BUG-001：评测数据必须与知识库权限一致）────────────────────────────
async def _check_kb_read(request: Request, db: AsyncSession, kb_id: uuid.UUID) -> None:
    """校验当前用户可读该知识库（owner/member/public/admin），否则 403。"""
    await require_kb_read(db, request.state.user, kb_id)


async def _check_kb_write(request: Request, db: AsyncSession, kb_id: uuid.UUID) -> None:
    """校验当前用户可写该知识库（owner/admin/editor），viewer 与越权访问 403。"""
    await require_kb_write(db, request.state.user, kb_id)


async def _accessible_filter(
    request: Request, db: AsyncSession
) -> set[uuid.UUID] | None:
    """列表接口的可见范围：admin 返回 None（不限），普通用户返回可访问 KB 集合。"""
    user = request.state.user
    if user.role == "admin":
        return None
    return await get_accessible_kb_ids(db, user)


async def _resolve_case_kb(db: AsyncSession, case_id: uuid.UUID) -> uuid.UUID:
    tc = await db.get(TestCase, case_id)
    if tc is None:
        raise NotFoundError("测试用例不存在")
    return tc.kb_id


def _parse_run_id(run_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(run_id)
    except (ValueError, AttributeError, TypeError):
        raise AppException(422, f"run_id 不是合法 UUID：{run_id!r}")


async def _resolve_run_kb(db: AsyncSession, run_id: uuid.UUID) -> uuid.UUID:
    run = await db.get(EvaluationRun, run_id)
    if run is None:
        raise NotFoundError("评估运行不存在")
    return run.kb_id


# BUG-024：字段长度必须与 PG 列宽一致，超长直接 422（而不是
# StringDataRightTruncation → 500）。列宽见 domain/models.py：
# question_type=32、experiment_name=128、retrieval_strategy/dataset_version=64。
_QUESTION_TYPE_MAX = 32
_EXPERIMENT_NAME_MAX = 128
_STRATEGY_MAX = 64
_DATASET_VERSION_MAX = 64
_IDEMPOTENCY_KEY_MAX = 128


class TestCaseItem(BaseModel):
    question: str = Field(min_length=1)
    golden_answer: str = Field(default="")
    golden_contexts: list[str] = Field(default_factory=list)
    # TASK-009：问题分类（受控词表见 QUESTION_TYPES），默认 general
    question_type: str = Field(default="general", max_length=_QUESTION_TYPE_MAX)
    dataset_version: str | None = Field(default=None, max_length=_DATASET_VERSION_MAX)
    # 未显式给出时：提供了 golden_answer 视为已确认，否则标记需人工确认
    needs_review: bool | None = None
    source_reference: str = Field(default="")


class EvaluationUploadRequest(BaseModel):
    """上传测试集：绑定目标知识库（检索范围与权限依据）。"""

    kb_id: uuid.UUID
    cases: list[TestCaseItem] = Field(min_length=1)
    # 测试集版本（如 tcm-v1）；用例未单独指定时使用此值
    dataset_version: str | None = Field(default=None, max_length=_DATASET_VERSION_MAX)


class EvaluationRunRequest(BaseModel):
    """运行评估：基于已上传测试集；可选 case_ids 子集与实验维度。"""

    kb_id: uuid.UUID
    case_ids: list[uuid.UUID] | None = None
    experiment_name: str = Field(
        default=DEFAULT_EXPERIMENT_NAME, max_length=_EXPERIMENT_NAME_MAX
    )
    # 阶段十二：可传 Strategy Registry 中的策略名（baseline_hybrid / herb_focused /
    # prescription_focused / theory_focused / literature_focused / multi_source）；
    # 历史标签（如 hybrid_rrf_rerank_hyde）不在注册表中，按 Baseline 行为执行。
    retrieval_strategy: str = Field(
        default=BASELINE_RETRIEVAL_STRATEGY, max_length=_STRATEGY_MAX
    )
    # 留空时按用例的 dataset_version 自动推断
    dataset_version: str | None = Field(default=None, max_length=_DATASET_VERSION_MAX)
    # 阶段十二：True = 逐条经 Query Analyzer + Dynamic Router 选择策略
    # （run 级 retrieval_strategy 记为 dynamic_router，逐条策略写入 results）
    use_dynamic_router: bool = False
    # 阶段十四：显式开关 Evidence Gate（None = 沿用全局 settings.EVIDENCE_GATE_ENABLED）
    # 用于「Gate 开 / 关」对照实验；关闭即完全回到阶段十三及之前的行为
    use_evidence_gate: bool | None = None
    # 阶段十五：显式开关 Self Reflection（None = 沿用 settings.SELF_REFLECTION_ENABLED）
    # 用于「Reflection ON / OFF」对照实验；关闭即完全回到阶段十四的行为
    use_self_reflection: bool | None = None
    # BUG-023：幂等键。同一 key 的重复提交直接返回既有运行，不再重复跑昂贵评测；
    # 也接受标准请求头 Idempotency-Key（两者取其一，body 优先）。
    idempotency_key: str | None = Field(default=None, max_length=_IDEMPOTENCY_KEY_MAX)


class TestCaseUpdateRequest(BaseModel):
    """人工确认/修订标准答案（TASK-009 录入机制）。"""

    golden_answer: str | None = None
    golden_contexts: list[str] | None = None
    source_reference: str | None = None
    question_type: str | None = Field(default=None, max_length=_QUESTION_TYPE_MAX)
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
    # 阶段十二：该用例实际使用的检索策略（动态路由运行逐条不同）
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


class QuestionTypeMetric(BaseModel):
    """按问题类型分组的指标（TASK-011 按问题类型分析检索策略效果）。"""

    question_type: str
    question_type_label: str
    case_count: int
    evaluated_count: int
    skipped_count: int
    # BUG-011：该组内因基础设施故障失败的用例数（不计入任何均值）
    failed_count: int = 0
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
    # BUG-011：因基础设施故障失败、未计入任何均值的用例数
    failed_count: int = 0
    # BUG-011：全部用例都失败 ⇒ 本轮指标不可信，前端应提示"评测基础设施异常"
    # 而不是展示一份看起来正常的 0 分报告
    infra_failed: bool = False
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
    # 阶段十四：Evidence Gate 归档（Gate 关闭时为 None）
    gate_decision: str | None = None
    gate_version: str | None = None
    retry_strategy: str | None = None
    # 阶段十五：Self Reflection 归档（Reflection 关闭时为 None）
    reflection_decision: str | None = None
    reflection_version: str | None = None
    reflection_retry_strategy: str | None = None
    reflection_reason: str | None = None


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
    # BUG-011：失败用例数与"整轮不可信"标记（历史运行 failed_count 为 0）
    failed_count: int = 0
    infra_failed: bool = False
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
        retrieval_strategy=r.retrieval_strategy,
        # 阶段十四：Gate 决策（Gate 关闭时为 None）
        gate_decision=r.gate_decision,
        gate_version=r.gate_version,
        retry_strategy=r.retry_strategy,
        # 阶段十五：Reflection 决策（Reflection 关闭时为 None）
        reflection_decision=r.reflection_decision,
        reflection_version=r.reflection_version,
        reflection_retry_strategy=r.reflection_retry_strategy,
        reflection_reason=r.reflection_reason,
    )


@router.get("/strategies")
async def list_strategies() -> dict:
    """阶段十二：可用检索策略清单（含生效参数，供实验选择与对比）。"""
    from src.application.retrieval_strategies import (
        BASELINE_STRATEGY,
        strategy_names,
    )

    return {
        "strategies": EvaluationService.list_strategies(),
        "default_strategy": BASELINE_STRATEGY,
        "names": strategy_names(),
    }


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
    payload: EvaluationUploadRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """上传测试集（问题-标准答案-上下文），写入 test_cases 表。

    AGENTS.md 建议测试集 ≥30 条；本端点不强制，由调用方保证质量。
    TASK-009：支持 question_type / dataset_version / needs_review / source_reference。

    权限（BUG-001）：需对该知识库有写权限（owner/admin/editor）。
    """
    await _check_kb_write(request, db, payload.kb_id)
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
    request: Request,
    dataset_version: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """查看指定知识库的测试集（含问题分类、数据集版本与人工确认状态）。

    权限（BUG-001）：需对该知识库有读权限，否则 403（不再向任意登录者暴露他人题库）。
    """
    await _check_kb_read(request, db, kb_id)
    service = EvaluationService(db)
    return await service.list_test_cases(kb_id, dataset_version)


@router.patch("/test-cases/{case_id}", response_model=TestCaseOut)
async def confirm_test_case(
    case_id: uuid.UUID,
    payload: TestCaseUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """人工确认/修订单条用例的标准答案。

    传入 golden_answer 后默认 needs_review=False（已确认），
    该用例在后续运行中才会计算 answer_correctness。

    权限（BUG-001）：按用例所属知识库校验写权限，禁止跨库/越权修改标准答案。
    """
    await _check_kb_write(request, db, await _resolve_case_kb(db, case_id))
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
    payload: EvaluationRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> EvaluationReport:
    """运行 RAGAS 评估：批量 RAG 问答 → 指标计算 → 入库 → 聚合报告 → 实验归档。

    权限（BUG-001）：需对该知识库有写权限，禁止对他人私有库发起（昂贵的）评测。

    BUG-011：失败用例不计入任何均值，报告额外给出 failed_count / infra_failed，
    基础设施故障不再伪装成"检索质量 0 分"。
    BUG-023：携带 idempotency_key（body 或 Idempotency-Key 头）可安全重试。
    """
    await _check_kb_write(request, db, payload.kb_id)
    service = EvaluationService(db)
    # BUG-023：幂等键支持两种写法，body 优先，其次标准请求头
    # BUG-024：请求头同样要做长度校验，否则超长会打到 PG 变成 500
    idempotency_key = payload.idempotency_key or request.headers.get("Idempotency-Key")
    if idempotency_key and len(idempotency_key) > _IDEMPOTENCY_KEY_MAX:
        raise AppException(
            422, f"idempotency_key 长度不得超过 {_IDEMPOTENCY_KEY_MAX} 字符"
        )
    run, results = await service.run(
        payload.kb_id,
        payload.case_ids,
        experiment_name=payload.experiment_name,
        retrieval_strategy=payload.retrieval_strategy,
        dataset_version=payload.dataset_version,
        use_dynamic_router=payload.use_dynamic_router,
        use_evidence_gate=payload.use_evidence_gate,
        use_self_reflection=payload.use_self_reflection,
        idempotency_key=idempotency_key or None,
    )
    cr, ac, passed = aggregate(results)
    failed_count = int(getattr(run, "failed_count", 0) or 0)
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
        failed_count=failed_count,
        infra_failed=infra_failed(failed_count, run.case_count),
        by_question_type=[
            QuestionTypeMetric(**item) for item in (run.by_question_type or [])
        ],
        results=[_to_case_out(r) for r in results],
    )


@router.get("/runs", response_model=list[EvaluationRunOut])
async def list_runs(
    kb_id: uuid.UUID | None = None,
    # BUG-023：原先硬编码 limit(100) 且无 offset；改为显式分页参数（默认值不变）
    limit: int = Query(default=DEFAULT_RUN_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    request: Request = None,  # noqa: B008 — 由 FastAPI 注入
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """实验运行归档列表（Baseline 与消融实验对比，按时间倒序）。

    权限（BUG-001）：指定 kb_id 时需可读；未指定时普通用户只返回其可访问 KB 的运行。
    """
    accessible = None
    if kb_id is not None:
        await _check_kb_read(request, db, kb_id)
    else:
        accessible = await _accessible_filter(request, db)
    service = EvaluationService(db)
    return await service.list_runs(
        kb_id, accessible_kb_ids=accessible, limit=limit, offset=offset
    )


@router.get("/runs/{run_id}", response_model=EvaluationRunOut)
async def get_run(
    run_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """单条实验运行归档详情（含配置快照与按问题类型指标）。

    权限（BUG-001）：按运行所属知识库校验读权限。
    """
    await _check_kb_read(request, db, await _resolve_run_kb(db, run_id))
    service = EvaluationService(db)
    return await service.get_run(run_id)


@router.get("/results", response_model=list[EvaluationHistoryItem])
async def list_results(
    kb_id: uuid.UUID | None = None,
    run_id: str | None = None,
    # BUG-023：原先硬编码 limit(200) 且无 offset；改为显式分页参数（默认值不变）
    limit: int = Query(default=DEFAULT_HISTORY_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """历史评估结果（关联 test_case 展示问题与标准答案，按时间倒序）。

    权限（BUG-001）：指定 kb_id / run_id 时校验读权限；否则按用户可见 KB 过滤。
    """
    accessible = None
    if kb_id is not None:
        await _check_kb_read(request, db, kb_id)
    elif run_id is not None:
        await _check_kb_read(request, db, await _resolve_run_kb(db, _parse_run_id(run_id)))
    else:
        accessible = await _accessible_filter(request, db)
    service = EvaluationService(db)
    return await service.list_history(
        kb_id, run_id, accessible_kb_ids=accessible, limit=limit, offset=offset
    )


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
