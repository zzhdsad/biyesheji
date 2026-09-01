"""评估指标（RAGAS 中文适配）单元测试。

不依赖数据库与真实模型：直接使用 MockLLM / MockEmbedding。MockLLM 返回非 JSON
文案，触发指标模块的 token 重叠回退路径，保证打分确定性可断言。
"""

from src.application.eval_metrics import (
    RAGASMetrics,
    _cosine,
    _parse_json,
    _token_f1,
)
from src.application.evaluation_service import aggregate
from src.application.evaluation_service import CaseResult
from src.infrastructure.embedding import MockEmbedding
from src.infrastructure.llm import MockLLM


def test_token_f1_identical_and_disjoint():
    assert _token_f1("公司考勤制度", "公司考勤制度") == 1.0
    assert _token_f1("abc", "xyz") == 0.0
    # 部分重叠：0 < score < 1
    partial = _token_f1("公司考勤制度", "公司报销制度")
    assert 0.0 < partial < 1.0


def test_cosine_identical_and_orthogonal():
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert _cosine([], [1.0]) == 0.0  # 空向量兜底


def test_parse_json_with_fences_and_noise():
    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('前缀噪声 {"b": 2} 后缀噪声') == {"b": 2}
    assert _parse_json("not json at all") is None
    assert _parse_json("") is None


def _metrics() -> RAGASMetrics:
    return RAGASMetrics(MockLLM(), MockEmbedding())


async def test_context_relevancy_fallback_token_hit():
    """MockLLM 输出非 JSON → 回退：问题 token 在上下文中的命中率。"""
    m = _metrics()
    # 上下文包含全部问题字符 → 命中率 1.0
    cr = await m.context_relevancy("公司考勤制度", ["公司考勤制度规定每日打卡"])
    assert cr == 1.0
    # 无关上下文 → 命中率 0.0
    cr0 = await m.context_relevancy("考勤", ["今日天气晴朗"])
    assert cr0 == 0.0
    # 空上下文 → 0.0
    assert await m.context_relevancy("任何问题", []) == 0.0


async def test_answer_correctness_identical_is_one():
    m = _metrics()
    ac = await m.answer_correctness("Q", "标准答案是考勤制度", "标准答案是考勤制度")
    # 完全相同：token F1=1，余弦=1 → 1.0
    assert ac == 1.0


async def test_answer_correctness_disjoint_below_one():
    m = _metrics()
    ac = await m.answer_correctness("Q", "完全不同的答案xyz", "标准答案是考勤制度abc")
    # 字符 token F1=0 → ac ≤ 0.5（语义余弦最多贡献 0.5）
    assert 0.0 <= ac < 1.0


def test_aggregate_pass_gate():
    cases = [
        CaseResult(
            test_case_id="1",
            question="q",
            golden_answer="g",
            context_relevancy=0.9,
            answer_correctness=0.8,
        ),
        CaseResult(
            test_case_id="2",
            question="q2",
            golden_answer="g2",
            context_relevancy=0.7,
            answer_correctness=0.7,
        ),
    ]
    cr, ac, passed = aggregate(cases)
    assert cr == 0.8  # (0.9+0.7)/2
    assert ac == 0.75  # (0.8+0.7)/2
    assert passed is True  # = 阈值 0.75 视为通过


def test_aggregate_fail_gate():
    cases = [
        CaseResult(
            test_case_id="1",
            question="q",
            golden_answer="g",
            context_relevancy=0.5,
            answer_correctness=0.4,
        )
    ]
    _, ac, passed = aggregate(cases)
    assert ac == 0.4
    assert passed is False


def test_aggregate_empty():
    cr, ac, passed = aggregate([])
    assert cr == 0.0 and ac == 0.0 and passed is False
