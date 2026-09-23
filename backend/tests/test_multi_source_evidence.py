"""阶段十：多源证据（统一 Evidence + 多来源分组）测试。

覆盖：
1. 单 Document Evidence / 2. 单 Resource Evidence
3~6. Herb / Prescription / Theory / Literature 四类 Resource Evidence
7. Document + Resource 混合分组 / 8. 多 Resource 混合分组
9. Evidence Level（分级规则 + 组/来源取最高分）
10. Citation 向后兼容（旧字段、旧结构不变）
11. /ask 返回 Evidence / 12. /ask-stream 返回 Evidence
13. Evidence 过滤 threshold（低于 RELEVANCE_THRESHOLD 不进 Evidence）
14. 同一来源多 chunk 聚合 + 分组排序 + 汇总计数
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.evidence import (
    EVIDENCE_HIGH_THRESHOLD,
    EVIDENCE_MEDIUM_THRESHOLD,
    RESOURCE_TYPE_LABELS,
    build_evidence,
    evidence_level,
    group_evidence,
    hit_to_evidence,
    package_evidence,
    summarize_evidence,
)
from src.application.rag_service import RagService
from src.infrastructure.embedding import MockEmbedding
from src.infrastructure.rerank import MockRerank
from tests.test_chat import _upload_doc
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE


# ── 辅助构造 ────────────────────────────────────────────────────────────────


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    db.scalars.return_value = result
    return db


def _svc() -> RagService:
    return RagService(_mock_db(), embedding=MockEmbedding(), rerank=MockRerank())


def _doc_hit(score=0.85, doc_name="伤寒论.pdf", chunk="chunk-doc"):
    return {
        "id": chunk,
        "doc_id": str(uuid.uuid4()),
        "kb_id": str(uuid.uuid4()),
        "content": "太阳病，头痛发热，汗出恶风",
        "page_num": 5,
        "title_path": "太阳病篇",
        "score": score,
        "dense_score": score,
        "rerank_score": score,
        "source_kind": "document",
        "doc_name": doc_name,
        "source_type": "经典古籍",
        "era": "汉",
        "credibility_level": 3,
    }


def _resource_hit(score=0.8, rtype="herb", rname="桂枝", chunk=None):
    return {
        "id": chunk or f"vec-{uuid.uuid4().hex[:8]}",
        "doc_id": "a" * 64,  # SHA256（Resource 非 UUID）
        "kb_id": str(uuid.uuid4()),
        "content": f"{rname} 的证据文本",
        "page_num": None,
        "title_path": None,
        "score": score,
        "dense_score": score,
        "rerank_score": score,
        "source_kind": "resource",
        "resource_type": rtype,
        "resource_id": str(uuid.uuid4()),
        "resource_name": rname,
        "doc_name": rname,
        "source_type": None,
        "era": None,
        "credibility_level": None,
    }


# ── 1. 单 Document Evidence ─────────────────────────────────────────────────


def test_document_evidence_unified_fields():
    """Document 命中 → 统一 Evidence：source_kind=document + 统一访问字段。"""
    hits = [_doc_hit()]
    evidence = build_evidence(hits)
    assert len(evidence) == 1
    ev = evidence[0]
    assert ev["source_kind"] == "document"
    assert ev["source_id"] == hits[0]["doc_id"]
    assert ev["source_name"] == "伤寒论.pdf"
    assert ev["source_label"] == "文档"
    assert ev["evidence_text"] == hits[0]["content"]
    assert ev["evidence_id"]
    assert ev["evidence_level"] == "high"
    # Resource 专属字段为 None（Document 不涉及）
    assert ev["resource_type"] is None
    assert ev["resource_id"] is None
    assert ev["resource_name"] is None


def test_document_evidence_grouped_into_document_group():
    """Document 证据 → 归入 document 组，组内来源即具体文档。"""
    groups = group_evidence(build_evidence([_doc_hit(), _doc_hit(score=0.6, doc_name="本草纲目.pdf", chunk="c2")]))
    assert [g["group_key"] for g in groups] == ["document"]
    assert groups[0]["source_label"] == "文档"
    assert groups[0]["source_count"] == 2  # 两个不同文档
    assert groups[0]["evidence_count"] == 2


# ── 2. 单 Resource Evidence ─────────────────────────────────────────────────


def test_resource_evidence_unified_fields():
    """Resource 命中 → 统一 Evidence：source_id/name 取 resource_id/resource_name。"""
    hit = _resource_hit(score=0.75, rtype="herb", rname="金银花")
    evidence = build_evidence([hit])
    assert len(evidence) == 1
    ev = evidence[0]
    assert ev["source_kind"] == "resource"
    assert ev["source_id"] == hit["resource_id"]
    assert ev["source_name"] == "金银花"
    assert ev["source_label"] == "中药"
    assert ev["resource_type"] == "herb"
    assert ev["evidence_level"] == "high"
    assert ev["evidence_text"] == hit["content"]


# ── 3~6. 四类 Resource Evidence ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "rtype,label",
    [("herb", "中药"), ("prescription", "方剂"), ("theory", "理论"), ("literature", "文献")],
)
def test_resource_types_evidence_and_labels(rtype, label):
    """herb / prescription / theory / literature 四类资源进入同一 Evidence 结构。"""
    hit = _resource_hit(score=0.72, rtype=rtype, rname=f"资源-{rtype}")
    ev = hit_to_evidence(hit, 1)
    assert ev["source_kind"] == "resource"
    assert ev["resource_type"] == rtype
    assert ev["source_label"] == label
    assert RESOURCE_TYPE_LABELS[rtype] == label
    # 四类共用同一套字段，不为每种资源单独建结构
    assert set(["evidence_id", "source_kind", "source_type", "source_id", "source_name",
                "evidence_text", "score", "evidence_level"]) <= set(ev)


def test_four_resource_types_form_four_groups():
    """四类资源同时命中 → 四个独立分组（可明确区分来源）。"""
    hits = [
        _resource_hit(score=0.8, rtype="herb", rname="金银花"),
        _resource_hit(score=0.78, rtype="prescription", rname="银翘散"),
        _resource_hit(score=0.76, rtype="theory", rname="卫气营血辨证"),
        _resource_hit(score=0.74, rtype="literature", rname="温病条辨"),
    ]
    groups = group_evidence(build_evidence(hits))
    assert [g["group_key"] for g in groups] == [
        "resource:herb",
        "resource:prescription",
        "resource:theory",
        "resource:literature",
    ]
    assert [g["source_label"] for g in groups] == ["中药", "方剂", "理论", "文献"]
    assert all(g["source_count"] == 1 and g["evidence_count"] == 1 for g in groups)


# ── 7. Document + Resource 混合 ─────────────────────────────────────────────


def test_document_and_resource_mixed_groups():
    """Document + Resource 混合 → 两组共存，组内证据互不混淆。"""
    hits = [
        _doc_hit(score=0.9),
        _resource_hit(score=0.82, rtype="herb", rname="金银花"),
        _resource_hit(score=0.7, rtype="literature", rname="本草纲目"),
    ]
    evidence = build_evidence(hits)
    groups = group_evidence(evidence)
    keys = [g["group_key"] for g in groups]
    assert set(keys) == {"document", "resource:herb", "resource:literature"}
    assert keys[0] == "document"  # 最高分在前

    doc_group = next(g for g in groups if g["group_key"] == "document")
    herb_group = next(g for g in groups if g["group_key"] == "resource:herb")
    assert doc_group["sources"][0]["source_name"] == "伤寒论.pdf"
    assert herb_group["sources"][0]["source_name"] == "金银花"
    assert herb_group["sources"][0]["evidences"][0]["evidence_level"] == "high"


def test_multi_resource_mixed_with_document():
    """方剂类问题场景：Prescription + Herb + Theory + Literature + Document。"""
    hits = [
        _resource_hit(score=0.88, rtype="prescription", rname="银翘散"),
        _resource_hit(score=0.8, rtype="herb", rname="金银花"),
        _resource_hit(score=0.66, rtype="herb", rname="连翘"),
        _resource_hit(score=0.55, rtype="theory", rname="辛凉解表"),
        _resource_hit(score=0.45, rtype="literature", rname="温病条辨"),
        _doc_hit(score=0.5),
    ]
    groups = group_evidence(build_evidence(hits))
    assert [g["group_key"] for g in groups] == [
        "resource:prescription",
        "resource:herb",
        "resource:theory",
        "document",
        "resource:literature",
    ]
    # herb 组：两个来源（金银花、连翘）聚在同一组
    herb_group = next(g for g in groups if g["group_key"] == "resource:herb")
    assert herb_group["source_count"] == 2
    assert [s["source_name"] for s in herb_group["sources"]] == ["金银花", "连翘"]


# ── 8. Evidence Level ───────────────────────────────────────────────────────


def test_evidence_level_rules():
    """沿用既有规则：>=0.7 high，>=0.3 medium，<0.3 insufficient。"""
    assert evidence_level(EVIDENCE_HIGH_THRESHOLD) == "high"
    assert evidence_level(0.95) == "high"
    assert evidence_level(EVIDENCE_MEDIUM_THRESHOLD) == "medium"
    assert evidence_level(0.69) == "medium"
    assert evidence_level(0.29) == "insufficient"


def test_group_and_source_level_from_max_score():
    """组/来源的 evidence_level 取组内最高分对应等级。"""
    hits = [
        _resource_hit(score=0.4, rtype="literature", rname="本草纲目", chunk="v1"),
        _resource_hit(score=0.75, rtype="literature", rname="本草纲目", chunk="v2"),
    ]
    # 同一来源（同一 resource_id）需显式复用，保证聚合到一条来源
    rid = str(uuid.uuid4())
    for h in hits:
        h["resource_id"] = rid
    groups = group_evidence(build_evidence(hits))
    assert len(groups) == 1
    group = groups[0]
    assert group["max_score"] == 0.75
    assert group["evidence_level"] == "high"
    assert group["sources"][0]["evidence_level"] == "high"
    assert group["sources"][0]["evidence_count"] == 2
    # 单条证据仍保留自己的等级
    levels = {e["evidence_level"] for e in group["sources"][0]["evidences"]}
    assert levels == {"medium", "high"}


# ── 9. Citation 向后兼容 ────────────────────────────────────────────────────


def test_citation_backward_compatible_fields():
    """旧 Citation 字段必须完整保留（旧 API / 旧前端 / 历史数据兼容）。"""
    hit = _doc_hit(score=0.8)
    citation = hit_to_evidence(hit, 1)
    for field in (
        "chunk_id",
        "source_index",
        "doc_id",
        "doc_name",
        "page_num",
        "title_path",
        "content",
        "score",
    ):
        assert field in citation, f"缺失兼容字段：{field}"
    assert citation["chunk_id"] == hit["id"]
    assert citation["source_index"] == 1
    assert citation["doc_name"] == "伤寒论.pdf"
    assert citation["page_num"] == 5
    assert citation["title_path"] == "太阳病篇"
    assert citation["score"] == 0.8
    # Stage 4-5 字段同样保留
    assert citation["source_type"] == "经典古籍"
    assert citation["era"] == "汉"
    assert citation["credibility_level"] == 3


def test_hits_to_citations_keeps_legacy_behavior():
    """_hits_to_citations / _build_citations 行为不变（编号 1-based、阈值过滤）。"""
    svc = _svc()
    hits = [
        {"id": "c1", "doc_id": "d1", "doc_name": "手册.pdf", "page_num": 3,
         "title_path": "考勤", "content": "工时八小时", "score": 0.9},
        {"id": "c2", "doc_id": "d2", "doc_name": "报销.pdf", "page_num": 1,
         "title_path": "报销", "content": "报销流程", "rerank_score": 0.6},
    ]
    cits = svc._hits_to_citations(hits)
    assert [c["source_index"] for c in cits] == [1, 2]
    assert cits[0]["chunk_id"] == "c1" and cits[1]["chunk_id"] == "c2"
    assert cits[1]["score"] == 0.6  # rerank_score 优先

    # 旧测试断言的 Resource Citation 扩展字段仍在
    res = svc._hits_to_citations([_resource_hit(score=0.85, rtype="prescription", rname="桂枝汤")])
    assert res[0]["source_kind"] == "resource"
    assert res[0]["resource_type"] == "prescription"
    assert res[0]["resource_name"] == "桂枝汤"
    assert res[0]["doc_name"] == "桂枝汤"


@pytest.mark.asyncio
async def test_build_citations_matches_stream_path_structure():
    """非流式（按标记解析）与流式（全量）两条路径产出同一 Evidence 结构。"""
    svc = _svc()
    hits = [_doc_hit(score=0.9), _resource_hit(score=0.7, rtype="herb", rname="桂枝")]
    by_marker = await svc._build_citations("答案[citation: 1, 5][citation: 2, 0]", hits)
    all_hits = svc._hits_to_citations(hits)
    assert [c["source_index"] for c in by_marker] == [1, 2]
    assert set(by_marker[0]) == set(all_hits[0])
    assert by_marker[1]["source_kind"] == "resource"
    assert by_marker[1]["source_label"] == "中药"


# ── 10. threshold 过滤 ──────────────────────────────────────────────────────


def test_evidence_filtered_by_threshold():
    """低于 RELEVANCE_THRESHOLD 的命中不进入 Evidence / 分组。"""
    import src.core.config as config_mod

    original = config_mod.settings.RELEVANCE_THRESHOLD
    config_mod.settings.RELEVANCE_THRESHOLD = 0.5
    try:
        hits = [
            _resource_hit(score=0.8, rtype="herb", rname="金银花"),
            _resource_hit(score=0.3, rtype="herb", rname="低分药"),
            _doc_hit(score=0.2),
        ]
        evidence = build_evidence(hits)
        assert len(evidence) == 1
        assert evidence[0]["source_name"] == "金银花"

        groups, summary = package_evidence(evidence)
        assert len(groups) == 1
        assert summary["evidence_count"] == 1
        assert summary["by_level"]["insufficient"] == 0
    finally:
        config_mod.settings.RELEVANCE_THRESHOLD = original


def test_empty_evidence_package():
    """无证据（拒答场景）：分组为空、汇总为 0，不报错。"""
    groups, summary = package_evidence([])
    assert groups == []
    assert summary["evidence_count"] == 0
    assert summary["source_count"] == 0
    assert summary["group_count"] == 0
    assert summary["max_score"] == 0


# ── 11. 汇总计数 ────────────────────────────────────────────────────────────


def test_summary_counts_and_levels():
    hits = [
        _resource_hit(score=0.9, rtype="herb", rname="金银花"),
        _resource_hit(score=0.5, rtype="herb", rname="连翘"),
        _doc_hit(score=0.8),
        _doc_hit(score=0.4, doc_name="本草纲目.pdf", chunk="c9"),
    ]
    evidence = build_evidence(hits)
    groups = group_evidence(evidence)
    summary = summarize_evidence(evidence, groups)
    assert summary["evidence_count"] == 4
    assert summary["source_count"] == 4
    assert summary["group_count"] == 2
    assert summary["max_score"] == 0.9
    assert summary["by_level"] == {"high": 2, "medium": 2, "insufficient": 0}


# ── 12. /ask 与 /ask-stream 返回 Evidence ───────────────────────────────────


class _MultiCiteLLM:
    """测试用 LLM：按命中数量输出多个 [citation: n] 标记。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def chat(self, messages: list[dict]) -> str:
        self.messages = messages
        return "多来源回答[citation: 1, 0][citation: 2, 0][citation: 3, 0]" \
               "[citation: 4, 0][citation: 5, 0]"

    async def chat_stream(self, messages: list[dict]):
        for piece in ["多来源回答", "[citation: 1, 0]", "[citation: 5, 0]"]:
            yield piece


_MIXED_HITS = [
    _resource_hit(score=0.88, rtype="prescription", rname="银翘散"),
    _doc_hit(score=0.82),
    _resource_hit(score=0.7, rtype="herb", rname="金银花"),
    _resource_hit(score=0.5, rtype="theory", rname="辛凉解表"),
    _resource_hit(score=0.4, rtype="literature", rname="温病条辨"),
]


def _patch_retrieve(monkeypatch, hits):
    """固定检索命中，保证 API 级多来源证据断言确定性（不改动检索算法）。"""

    async def _fake_retrieve(
        self, kb_ids, question, strategy=None, resource_types=None, analysis=None,
    ):  # noqa: ARG001
        # 仍走真实组件装配（_ensure_components），仅固定命中结果
        await RagService._ensure_components(self)
        return [dict(h) for h in hits]

    monkeypatch.setattr(RagService, "_retrieve", _fake_retrieve)


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_returns_evidence_and_groups(client, monkeypatch):
    """POST /chat/ask：返回 citations + evidence + evidence_groups + summary。"""
    import src.application.rag_service as rag_mod

    _patch_retrieve(monkeypatch, _MIXED_HITS)
    monkeypatch.setattr(rag_mod, "get_llm", lambda config: _MultiCiteLLM())

    kb_id = _make_kb(client)
    resp = client.post(
        "/api/v1/chat/ask", json={"question": "银翘散的组成与出处？", "kb_ids": [kb_id]}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # 旧结构不变
    assert data["answer"] and data["citations"]
    assert data["citations"][0]["doc_name"]
    assert data["citations"][0]["source_index"] == 1

    # 新结构：统一 Evidence
    assert len(data["evidence"]) == 5
    for ev in data["evidence"]:
        assert ev["evidence_id"] and ev["source_id"] and ev["source_name"]
        assert ev["source_label"] and ev["evidence_text"] and ev["evidence_level"]

    # 多来源分组：方剂 + 文档 + 中药 + 理论 + 文献
    keys = [g["group_key"] for g in data["evidence_groups"]]
    assert keys == [
        "resource:prescription",
        "document",
        "resource:herb",
        "resource:theory",
        "resource:literature",
    ]
    assert data["evidence_summary"]["evidence_count"] == 5
    assert data["evidence_summary"]["group_count"] == 5
    assert data["evidence_summary"]["source_count"] == 5


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_stream_returns_evidence_and_groups(client, monkeypatch):
    """POST /chat/ask-stream：citations 事件携带 evidence / evidence_groups。"""
    import json

    _patch_retrieve(monkeypatch, _MIXED_HITS)

    kb_id = _make_kb(client)
    resp = client.post(
        "/api/v1/chat/ask-stream", json={"question": "银翘散的组成与出处？", "kb_ids": [kb_id]}
    )
    assert resp.status_code == 200, resp.text

    events = []
    event = ""
    data = ""
    for line in resp.text.split("\n"):
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            data += line[6:]
        elif line == "":
            if event and data:
                events.append((event, json.loads(data)))
            event = ""
            data = ""

    assert [e for e, _ in events][0] == "start"
    cit = next(d for e, d in events if e == "citations")
    # 向后兼容：citations 键仍在
    assert len(cit["citations"]) == 5
    # 阶段十：evidence / evidence_groups / evidence_summary
    assert len(cit["evidence"]) == 5
    assert [g["group_key"] for g in cit["evidence_groups"]] == [
        "resource:prescription",
        "document",
        "resource:herb",
        "resource:theory",
        "resource:literature",
    ]
    assert cit["evidence_summary"]["evidence_count"] == 5
    # 事件序列不变：start → citations → delta × N → done
    names = [e for e, _ in events]
    assert names[-1] == "done"
    assert "delta" in names


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_real_document_evidence(client):
    """真实链路（上传文档→检索→生成）：证据为 document 组且字段完整。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id, "考勤制度.txt")
    resp = client.post(
        "/api/v1/chat/ask", json={"question": "公司实行什么工时制度？", "kb_ids": [kb_id]}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["evidence"], "真实链路应产出 Evidence"
    assert [g["group_key"] for g in data["evidence_groups"]] == ["document"]
    ev = data["evidence"][0]
    assert ev["source_name"] == "考勤制度.txt"
    assert ev["source_label"] == "文档"
    assert ev["evidence_level"] in {"high", "medium", "insufficient"}
    # 持久化仍为旧结构（历史数据兼容，无新增迁移）
    messages = client.get(
        f"/api/v1/chat/conversations/{data['conversation_id']}/messages"
    ).json()
    assert messages[1]["citations"]["sources"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_ask_stream_real_document_evidence(client):
    """真实流式链路：citations 事件同时含 citations 与 evidence 分组。"""
    import json

    kb_id = _make_kb(client)
    _upload_doc(client, kb_id, "考勤制度.txt")
    resp = client.post(
        "/api/v1/chat/ask-stream", json={"question": "公司实行什么工时制度？", "kb_ids": [kb_id]}
    )
    assert resp.status_code == 200, resp.text
    cit_data = ""
    for line in resp.text.split("\n"):
        if line.startswith("event: citations"):
            continue
        if line.startswith("data: ") and '"citations"' in line:
            cit_data = line[6:]
            break
    payload = json.loads(cit_data)
    assert payload["citations"]
    assert payload["evidence"]
    assert payload["evidence_groups"][0]["group_key"] == "document"
    assert payload["evidence_summary"]["evidence_count"] == len(payload["evidence"])
