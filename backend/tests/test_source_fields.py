"""来源可信度标注功能测试（AGENTS.md 中医约束第 6 条）。

单元：固定映射与受控枚举、InMemoryVectorStore 来源字段透传。
集成（需 PostgreSQL）：
- 上传带来源标注 → documents/chunks/向量行三处一致
- 枚举外取值 400
- 历史文档批量补标（documents + chunks + completed 文档自动重向量化）
"""

import uuid

import pytest

from src.core.source_meta import (
    CREDIBILITY_MAP,
    ERAS,
    SOURCE_TYPES,
    credibility_for,
    is_valid_era,
    is_valid_source_type,
)
from src.infrastructure.milvus_store import InMemoryVectorStore, VectorRow
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE


# ---------- 单元：映射与枚举 ----------


def test_credibility_mapping_is_fixed():
    assert CREDIBILITY_MAP == {
        "国家标准": 5,
        "规划教材": 4,
        "经典古籍": 3,
        "后世医家": 2,
        "民间偏方": 1,
    }
    assert credibility_for("经典古籍") == 3
    assert credibility_for(None) is None


def test_enum_validation_rejects_outside_values():
    assert is_valid_source_type("规划教材")
    assert is_valid_source_type(None)  # 未标注允许（历史文档）
    assert not is_valid_source_type("江湖游医")
    assert is_valid_era("汉")
    assert not is_valid_era("民国")
    assert len(SOURCE_TYPES) == 5 and len(ERAS) == 7


# ---------- 单元：InMemoryVectorStore 来源字段透传 ----------


def _row(source_type=None, level=None) -> VectorRow:
    return VectorRow(
        id="c1",
        doc_id="d1",
        kb_id="kb1",
        chunk_index=0,
        content="太阳病，头痛发热",
        page_num=None,
        title_path=None,
        dense_vector=[0.1] * 8,
        sparse_vector={1: 0.5},
        source_type=source_type,
        credibility_level=level,
    )


def test_inmemory_store_roundtrips_source_fields():
    store = InMemoryVectorStore()
    store.insert([_row("经典古籍", 3)])
    row = store.query_rows("d1")[0]
    assert row["source_type"] == "经典古籍"
    assert row["credibility_level"] == 3
    hit = store.search([0.1] * 8, ["kb1"], 5)[0]
    assert hit["source_type"] == "经典古籍"
    assert hit["credibility_level"] == 3


def test_inmemory_store_unlabeled_fields_are_none():
    store = InMemoryVectorStore()
    store.insert([_row()])
    hit = store.search([0.1] * 8, ["kb1"], 5)[0]
    assert hit["source_type"] is None
    assert hit["credibility_level"] is None


# ---------- 集成：上传标注 → 三处冗余一致 ----------


def _upload_txt(client, kb_id, name, form=None):
    text = "# 伤寒论\n\n" + "\n\n".join(f"第{i}条，" + "太阳病" * 100 for i in range(3))
    resp = client.post(
        "/api/v1/documents/upload",
        data=form or {"kb_id": kb_id},
        files={"file": (name, text.encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_upload_with_source_labels_and_derives_level(client, vector_store):
    kb_id = _make_kb(client)
    doc_id = _upload_txt(
        client,
        kb_id,
        "伤寒论.txt",
        form={
            "kb_id": kb_id,
            "source_type": "经典古籍",
            "era": "汉",
        },
    )

    detail = client.get(f"/api/v1/documents/{doc_id}").json()
    assert detail["source_type"] == "经典古籍"
    assert detail["era"] == "汉"
    assert detail["credibility_level"] == 3  # 后端固定映射推导，非表单传入

    chunks = client.get(f"/api/v1/documents/{doc_id}/chunks").json()
    assert chunks, "自动解析应已产出切片"
    assert all(c["source_type"] == "经典古籍" for c in chunks)
    assert all(c["credibility_level"] == 3 for c in chunks)

    # 向量行：内存实现必然带回；真实 Milvus 仅动态字段集合带回（老集合降级跳过）
    rows = vector_store.query_rows(doc_id)
    if isinstance(vector_store, InMemoryVectorStore) or vector_store._supports_dynamic_fields():
        assert rows and all(r["source_type"] == "经典古籍" for r in rows)
        assert rows and all(r["credibility_level"] == 3 for r in rows)


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_upload_rejects_invalid_source_enum(client):
    kb_id = _make_kb(client)
    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id, "source_type": "网络段子", "era": "汉"},
        files={"file": ("a.txt", "太阳病".encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 400
    assert "来源类型" in resp.json()["message"]


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_upload_rejects_invalid_era_enum(client):
    kb_id = _make_kb(client)
    resp = client.post(
        "/api/v1/documents/upload",
        data={"kb_id": kb_id, "source_type": "经典古籍", "era": "民国"},
        files={"file": ("a.txt", "太阳病".encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 400
    assert "年代" in resp.json()["message"]


# ---------- 集成：历史文档批量补标 ----------


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_backfill_source_by_doc_id(client):
    kb_id = _make_kb(client)
    # 旧数据：上传时不带任何来源标注
    doc_id = _upload_txt(client, kb_id, "旧教材.txt")
    detail = client.get(f"/api/v1/documents/{doc_id}").json()
    assert detail["source_type"] is None
    assert detail["parse_status"] == "completed"

    resp = client.post(
        "/api/v1/documents/backfill-source",
        json={"source_type": "规划教材", "era": "现代", "doc_ids": [doc_id]},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["updated"] == 1
    assert doc_id in result["doc_ids"]
    # completed 文档需重向量化更新 Milvus，应进入派发列表
    assert doc_id in result["reindex_doc_ids"]

    detail = client.get(f"/api/v1/documents/{doc_id}").json()
    assert detail["source_type"] == "规划教材"
    assert detail["era"] == "现代"
    assert detail["credibility_level"] == 4
    chunks = client.get(f"/api/v1/documents/{doc_id}/chunks").json()
    assert all(c["source_type"] == "规划教材" for c in chunks)
    assert all(c["credibility_level"] == 4 for c in chunks)


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_backfill_source_by_kb_batch(client):
    kb_id = _make_kb(client)
    id1 = _upload_txt(client, kb_id, "批次甲.txt")
    id2 = _upload_txt(client, kb_id, "批次乙.txt")

    resp = client.post(
        "/api/v1/documents/backfill-source",
        json={"source_type": "后世医家", "era": "清", "kb_id": kb_id},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 2
    for did in (id1, id2):
        detail = client.get(f"/api/v1/documents/{did}").json()
        assert detail["source_type"] == "后世医家"
        assert detail["credibility_level"] == 2


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_backfill_requires_filter_and_valid_enum(client):
    # 无筛选条件 → 400
    resp = client.post(
        "/api/v1/documents/backfill-source",
        json={"source_type": "规划教材"},
    )
    assert resp.status_code == 400
    # 枚举外取值 → 400
    resp = client.post(
        "/api/v1/documents/backfill-source",
        json={"source_type": "不存在的类型", "kb_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 400
