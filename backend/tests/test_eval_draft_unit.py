"""评测参考答案草稿的纯单元测试（不依赖 PostgreSQL / Milvus / LLM）。

覆盖人工确认前的安全边界：
- 草稿只返回，不写入、不自动确认；
- 引用标记从标准答案中剥离；
- 检索拒答时不生成可用草稿。
"""

import uuid

from src.application.evaluation_service import EvaluationService
from src.domain.models import TestCase as EvalCase


class _FakeDB:
    def __init__(self, case: EvalCase) -> None:
        self.case = case

    async def get(self, _model, _case_id):  # noqa: ANN001
        return self.case


class _FakeRag:
    def __init__(self, answer: str, hits: list[dict]) -> None:
        self.answer = answer
        self.hits = hits

    async def retrieve_and_answer(self, _kb_ids, _question, **_kwargs):  # noqa: ANN001
        return self.answer, self.hits


def _case() -> EvalCase:
    return EvalCase(
        kb_id=uuid.uuid4(),
        question="黄芪有什么功效？",
        golden_answer="",
        golden_contexts=[],
        question_type="herb",
        dataset_version="tcm-v1",
        needs_review=True,
        source_reference="",
    )


def _service(answer: str, hits: list[dict]) -> EvaluationService:
    svc = object.__new__(EvaluationService)
    svc.db = _FakeDB(_case())
    svc.rag = _FakeRag(answer, hits)
    return svc


async def test_draft_strips_citation_but_does_not_confirm():
    svc = _service(
        "黄芪补气 [citation: 1, 2]。",
        [
            {
                "content": "黄芪味甘，性微温，功能补气升阳。",
                "doc_name": "中药资源库",
                "title_path": "黄芪",
                "page_num": 2,
            }
        ],
    )

    out = await svc.draft_golden_answer(uuid.uuid4())

    assert out["usable"] is True
    assert out["answer"] == "黄芪补气。"
    assert out["golden_contexts"] == ["黄芪味甘，性微温，功能补气升阳。"]
    assert "中药资源库" in out["source_reference"]
    # 只返回草稿：用例仍待人工确认，标准答案仍为空
    assert svc.db.case.needs_review is True
    assert svc.db.case.golden_answer == ""


async def test_draft_refusal_is_not_usable():
    svc = _service("根据现有资料，我无法回答该问题。", [])

    out = await svc.draft_golden_answer(uuid.uuid4())

    assert out["usable"] is False
    assert out["answer"] == ""
    assert out["warning"]
