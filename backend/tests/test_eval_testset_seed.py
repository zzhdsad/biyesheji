"""中医评测测试集种子文件校验（TASK-009，不依赖数据库）。

约束（AGENTS.md / TASKS.md）：
- 规模 ≥30 条（论文实验可用规模，建议 30～50）
- 覆盖六类中医知识问题：中药 / 方剂 / 中医理论 / 文献出处 / 多来源 / 无依据
- 不得凭空编造中医医学事实作为标准答案：
  知识类问题 golden_answer 留空且 needs_review=true，
  unanswerable 类给出系统拒答行为预期（非医学事实）
"""

import json
from pathlib import Path

from src.application.evaluation_service import QUESTION_TYPES

_SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "tcm_eval_seed_v1.json"

REQUIRED_TYPES = {
    "herb",
    "prescription",
    "theory",
    "literature",
    "multi_source",
    "unanswerable",
}


def _load() -> dict:
    assert _SEED_FILE.exists(), f"缺少测试集文件：{_SEED_FILE}"
    with _SEED_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def test_seed_size_at_least_30():
    """规模 ≥30 条。"""
    cases = _load()["cases"]
    assert len(cases) >= 30, f"测试集规模不足：{len(cases)}"
    assert len(cases) <= 50, f"测试集规模超出建议范围：{len(cases)}"


def test_seed_questions_unique_and_non_empty():
    """问题非空且不重复。"""
    questions = [c["question"] for c in _load()["cases"]]
    assert all(q.strip() for q in questions), "存在问题为空"
    assert len(set(questions)) == len(questions), "存在问题重复"


def test_seed_question_types_valid_and_covered():
    """问题类型合法且覆盖六类中医知识问题。"""
    cases = _load()["cases"]
    for c in cases:
        assert c["question_type"] in QUESTION_TYPES, f"非法问题类型：{c['question_type']}"
    covered = {c["question_type"] for c in cases}
    assert REQUIRED_TYPES <= covered, f"缺少必需问题类型：{REQUIRED_TYPES - covered}"


def test_seed_no_fabricated_golden_answers():
    """知识类问题不预填标准答案，拒答类给出系统行为预期。"""
    for c in _load()["cases"]:
        if c["question_type"] == "unanswerable":
            assert c["needs_review"] is False
            assert c["golden_answer"].strip(), "拒答用例需给出系统行为预期答案"
        else:
            assert c["golden_answer"] == "", "知识类问题不得预填未核实的标准答案"
            assert c["needs_review"] is True
            assert c["source_reference"].strip(), "需说明人工标注依据"


def test_seed_metadata_documented():
    """文件元信息完整：数据集版本 + 标注策略说明。"""
    payload = _load()
    assert payload["dataset_version"]
    assert payload.get("annotation_policy"), "需说明标注策略与人工确认要求"
    assert payload.get("question_types")
