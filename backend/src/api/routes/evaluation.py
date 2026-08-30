"""评估路由：上传测试集、运行评估、查看报告。"""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/evaluation", tags=["evaluation"])


class TestCaseItem(BaseModel):
    question: str
    golden_answer: str
    golden_contexts: list[str] = Field(default_factory=list)


class EvaluationReport(BaseModel):
    run_id: str
    case_count: int
    context_relevancy: float
    answer_correctness: float
    # 质量门禁：AGENTS.md 要求准确率 ≥ 75% 才允许合入
    passed: bool


@router.post("/upload")
async def upload_test_set(cases: list[TestCaseItem]) -> dict:
    """上传测试集（问题-标准答案-上下文三元组，≥30 条）。

    TODO: 写入 test_cases 表并返回 case_id 列表。
    """
    return {"uploaded": len(cases)}


@router.post("/run", response_model=EvaluationReport)
async def run_evaluation(kb_id: str) -> EvaluationReport:
    """运行 RAGAS 评估（占位实现）。

    TODO: 批量执行问答 → 计算 Context Relevancy / Answer Correctness / Faithfulness
          → 写入 evaluation_results 表 → 生成可视化报告。
    """
    run_id = str(uuid.uuid4())
    return EvaluationReport(
        run_id=run_id,
        case_count=0,
        context_relevancy=0.0,
        answer_correctness=0.0,
        passed=False,
    )


@router.get("/results")
async def list_results(kb_id: str | None = None) -> list[dict]:
    """历史评估结果列表。TODO: 查询 evaluation_results 表。"""
    return []
