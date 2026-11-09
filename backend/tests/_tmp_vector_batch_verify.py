"""P0-1 针对性验证：结构化向量化分批处理（纯合成数据，不连真实 DB/Milvus/模型）。

运行：docker exec -e PYTHONPATH=/app kp-backend python /app/tests/_tmp_vector_batch_verify.py
覆盖：①键集分批无重复遗漏 ②每批独立编码/写入 ③中途写入失败旧向量保留
④普通文档 replace_doc 与 Milvus insert 契约不变；另含组路径/重复主键回退/空记录分支。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from types import SimpleNamespace

from sqlalchemy.sql import Update, operators
from sqlalchemy.sql.elements import BinaryExpression

from src.application.structured_vector_service import (
    StructuredVectorService,
    structured_doc_id,
    structured_group_doc_id,
    structured_group_vector_id,
    structured_vector_id,
)
from src.core.config import settings
from src.core.file_spaces import STRUCTURED_SPACE
from src.domain.models import (
    STRUCTURED_GROUP_CONFIRMED,
    STRUCTURED_PARSE_SUCCESS,
    Document,
    StructuredGroup,
)
from src.infrastructure.embedding import MockEmbedding
from src.infrastructure.milvus_store import (
    InMemoryVectorStore,
    MilvusStore,
    VectorRow,
    VectorStoreError,
)

CHECKS = 0


def ok(cond, label):
    global CHECKS
    if not cond:
        raise AssertionError(label)
    CHECKS += 1
    print(f"  PASS {label}")


# ── 假会话：SELECT 按语句的 limit / seq_no 键集谓词过滤；UPDATE 只记录状态 ──


def _keyset_bound(stmt):
    where = stmt.whereclause
    if where is None:
        return None
    stack = [where]
    while stack:
        c = stack.pop()
        if isinstance(c, BinaryExpression) and c.operator is operators.gt:
            if getattr(c.left, "name", None) == "seq_no":
                v = getattr(c.right, "effective_value", None)
                return getattr(c.right, "value", None) if v is None else v
        stack.extend(c.get_children())
    return None


def _limit_value(stmt):
    lc = getattr(stmt, "_limit_clause", None)
    if lc is None:
        return 10**9
    v = getattr(lc, "effective_value", None)
    return getattr(lc, "value", None) if v is None else v


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, doc=None, parse=None, group=None, records=(), group_records=()):
        self.doc, self.parse, self.group = doc, parse, group
        self.records = list(records)
        self.group_records = list(group_records)
        self.status_log = []
        self.select_sizes = []

    async def get(self, model, pk):
        return self.doc if model is Document else self.group

    async def scalar(self, stmt):
        return self.parse

    async def execute(self, stmt):
        if isinstance(stmt, Update):
            params = stmt.compile().params
            self.status_log.append({k: v for k, v in params.items() if k.startswith("vector")})
            return FakeResult([])
        table = stmt.get_final_froms()[0].name
        rows = self.records if table == "structured_records" else self.group_records
        bound = _keyset_bound(stmt)
        picked = sorted(
            (r for r in rows if bound is None or r.seq_no > bound),
            key=lambda r: r.seq_no,
        )[: _limit_value(stmt)]
        self.select_sizes.append(len(picked))
        return FakeResult(picked)

    async def commit(self):
        pass


# ── 假向量库 / 假 Embedding ────────────────────────────────────────────────


class TrackingStore(InMemoryVectorStore):
    """InMemory（同主键覆盖）+ 调用轨迹。"""

    def __init__(self):
        super().__init__()
        self.write_sizes = []
        self.flush_calls = 0
        self.delete_ids_calls = []
        self.delete_doc_calls = []

    def insert_batch(self, rows, *, flush=True):
        self.write_sizes.append(len(rows))
        return super().insert_batch(rows, flush=flush)

    def flush(self):
        self.flush_calls += 1

    def delete_by_ids(self, ids):
        self.delete_ids_calls.append(list(ids))
        super().delete_by_ids(ids)

    def delete_by_doc(self, doc_id):
        self.delete_doc_calls.append(doc_id)
        super().delete_by_doc(doc_id)


class DupStore(InMemoryVectorStore):
    """模拟 Milvus insert 语义：同主键不覆盖、追加重复行。"""

    def __init__(self, fail_on_write=None):
        super().__init__()
        self._dups = []
        self.fail_on_write = fail_on_write
        self.write_sizes = []
        self.flush_calls = 0
        self.delete_ids_calls = []
        self.delete_doc_calls = 0
        self._writes = 0

    def insert(self, rows):
        self._writes += 1
        if self.fail_on_write is not None and self._writes >= self.fail_on_write:
            raise VectorStoreError("模拟 Milvus 写入失败")
        self.write_sizes.append(len(rows))
        self._dups.extend(rows)
        return len(rows)

    def count_by_doc(self, doc_id):
        return sum(1 for r in self._dups if r.doc_id == doc_id)

    def ids_by_doc(self, doc_id):
        return [r.id for r in self._dups if r.doc_id == doc_id]

    def delete_by_doc(self, doc_id):
        self.delete_doc_calls += 1
        self._dups = [r for r in self._dups if r.doc_id != doc_id]

    def delete_by_ids(self, ids):
        self.delete_ids_calls.append(list(ids))
        drop = set(ids)
        self._dups = [r for r in self._dups if r.id not in drop]

    def flush(self):
        self.flush_calls += 1


class TrackingEmbedding(MockEmbedding):
    def __init__(self):
        super().__init__(dim=8)
        self.calls = []

    def encode(self, texts):
        self.calls.append(len(texts))
        return super().encode(texts)


def make_records(n, empty_every=7):
    return [
        SimpleNamespace(
            id=uuid.uuid4(),
            seq_no=i * 2,  # 故意留空档，验证键集分页不依赖连续 seq
            knowledge_text="" if i % empty_every == 0 else f"名称：记录{i}；内容：数据{i}",
            row_no=i + 1,
            sheet_name="Sheet1",
            json_path=None,
        )
        for i in range(n)
    ]


def make_doc(doc_id):
    return SimpleNamespace(
        id=doc_id, space=STRUCTURED_SPACE, deleted_at=None, kb_id=uuid.uuid4(), file_name="合成.tsv"
    )


def old_row(vid, doc_key, content="old"):
    return VectorRow(
        id=vid, doc_id=doc_key, kb_id="kb", chunk_index=0, content=content,
        page_num=None, title_path=None, dense_vector=[0.0] * 8,
    )


# ── 验证用例 ──────────────────────────────────────────────────────────────


async def test_document_batching():
    print("[1] 文件向量化：键集覆盖 / 每批独立编码写入 / 状态机")
    doc_id = uuid.uuid4()
    records = make_records(1234)
    usable = [r for r in records if r.knowledge_text.strip()]
    fake = FakeSession(
        doc=make_doc(doc_id),
        parse=SimpleNamespace(status=STRUCTURED_PARSE_SUCCESS, vector_count=0),
        records=records,
    )
    store, emb = TrackingStore(), TrackingEmbedding()
    svc = StructuredVectorService(db=fake, embedding=emb, store=store)
    total = await svc.vectorize_document(doc_id)

    ok(total == len(usable), f"返回条数={total} == 有效记录数")
    ok(sum(fake.select_sizes) == len(records), "分批读取总行数==记录总数（无重复无遗漏）")
    ok(max(fake.select_sizes) <= 100 and fake.select_sizes[-1] < 100, "每批读取≤批大小且末批短批收尾")
    chunk = max(int(settings.VECTORIZE_ENCODE_BATCH_SIZE or 0), 1)
    expected_sizes = []
    for i in range(0, len(records), 100):
        n = sum(1 for r in records[i : i + 100] if r.knowledge_text.strip())
        for j in range(0, n, chunk):
            expected_sizes.append(min(chunk, n - j))
    ok(
        store.write_sizes == expected_sizes,
        f"每 {chunk} 条一批独立写入（末批短批收尾，共 {len(expected_sizes)} 批）",
    )
    ok(all(s <= chunk for s in emb.calls), "每次 Embedding 调用≤VECTORIZE_ENCODE_BATCH_SIZE")
    ok(sum(emb.calls) == len(usable), "Embedding 总条数==有效记录数")
    ok(len(emb.calls) == len(expected_sizes), "每次 Embedding 调用对应一个写入批次")
    doc_key = structured_doc_id(doc_id)
    ok(store.count_by_doc(doc_key) == len(usable), "入库条数==有效记录数")
    ok(
        {r.id for r in store.all_rows() if r.doc_id == doc_key} == {structured_vector_id(r.id) for r in usable},
        "向量 id 集合完全一致（无重复无遗漏）",
    )
    states = [s["vector_status"] for s in fake.status_log]
    ok(states[0] == "pending" and states[1] == "indexing" and states[-1] == "completed",
       "状态 pending→indexing→…→completed")
    ok(all(s == "indexing" for s in states[1:-1]), "中间进度回写均为 indexing")
    ok(fake.status_log[-1]["vector_count"] == len(usable) and "vectorized_at" in fake.status_log[-1], "完成状态写回条数与时间")
    ok(store.delete_ids_calls == [] and store.delete_doc_calls == [], "首次向量化无旧向量可清理")


async def test_mid_write_failure():
    print("[2] 中途某批写入失败：旧向量保留、不做任何删除")
    doc_id = uuid.uuid4()
    records = make_records(300)
    usable = [r for r in records if r.knowledge_text.strip()]
    doc_key = structured_doc_id(doc_id)
    store = DupStore(fail_on_write=2)  # 第 2 批写入失败（Milvus 追加语义）
    old = [old_row(structured_vector_id(r.id), doc_key) for r in usable[:40]]
    old += [old_row(f"sr:stale-{i}", doc_key) for i in range(10)]
    store.insert(old)
    fake = FakeSession(
        doc=make_doc(doc_id),
        parse=SimpleNamespace(status=STRUCTURED_PARSE_SUCCESS, vector_count=len(old)),
        records=records,
    )
    svc = StructuredVectorService(db=fake, embedding=TrackingEmbedding(), store=store)
    total = await svc.vectorize_document(doc_id)
    ok(total == 0, "失败返回 0")
    ok(store.delete_ids_calls == [] and store.delete_doc_calls == 0, "失败前后未执行任何删除")
    survivors = [r for r in store._dups if r.content == "old"]
    ok(len(survivors) == len(old), "全部旧向量仍在（含重叠 id 与过期 id）")
    ok(len(store.write_sizes) == 1, "仅第 1 批写入成功即中止")
    ok(fake.status_log[-1]["vector_status"] == "failed", "状态置 failed")
    ok("模拟 Milvus 写入失败" in fake.status_log[-1]["vector_error"], "失败原因如实记录")


async def test_duplicate_pk_fallback():
    print("[3] Milvus 重复主键语义：计数校验触发清旧写新回退，终态无重复")
    doc_id = uuid.uuid4()
    records = make_records(250)
    usable = [r for r in records if r.knowledge_text.strip()]
    doc_key = structured_doc_id(doc_id)
    store = DupStore()
    old = [old_row(structured_vector_id(r.id), doc_key) for r in usable[:30]]
    old += [old_row(f"sr:stale-{i}", doc_key) for i in range(5)]
    store.insert(old)
    fake = FakeSession(
        doc=make_doc(doc_id),
        parse=SimpleNamespace(status=STRUCTURED_PARSE_SUCCESS, vector_count=len(old)),
        records=records,
    )
    emb = TrackingEmbedding()
    svc = StructuredVectorService(db=fake, embedding=emb, store=store)
    total = await svc.vectorize_document(doc_id)
    ok(total == len(usable), "回退后返回条数==有效记录数")
    ok(store.count_by_doc(doc_key) == len(usable), "终态条数正确")
    ids = store.ids_by_doc(doc_key)
    ok(len(ids) == len(set(ids)), "终态无重复主键")
    ok(set(ids) == {structured_vector_id(r.id) for r in usable}, "终态 id 集合正确（过期旧行已清）")
    ok(store.delete_doc_calls == 1, "重复行走了一次清旧写新回退")
    ok(sum(emb.calls) == 2 * len(usable), "回退重放批次（重读+重编码）")
    ok(fake.status_log[-1]["vector_status"] == "completed", "回退成功后状态 completed")


async def test_empty_records():
    print("[4] 全空记录：清掉旧向量并 completed 0")
    doc_id = uuid.uuid4()
    doc_key = structured_doc_id(doc_id)
    records = make_records(5, empty_every=1)
    store = TrackingStore()
    store.insert([old_row(f"sr:stale-{i}", doc_key) for i in range(3)])
    fake = FakeSession(
        doc=make_doc(doc_id),
        parse=SimpleNamespace(status=STRUCTURED_PARSE_SUCCESS, vector_count=3),
        records=records,
    )
    svc = StructuredVectorService(db=fake, embedding=TrackingEmbedding(), store=store)
    total = await svc.vectorize_document(doc_id)
    ok(total == 0 and store.count_by_doc(doc_key) == 0, "无有效记录→旧向量清空")
    ok(fake.status_log[-1]["vector_status"] == "completed" and fake.status_log[-1]["vector_count"] == 0, "completed 0")


async def test_group_batching():
    print("[5] 关联组：分批覆盖 + 未确认组守卫")
    group_id = uuid.uuid4()
    group_key = structured_group_doc_id(group_id)
    records = [
        SimpleNamespace(id=uuid.uuid4(), seq_no=i, knowledge_text=f"学生：{i}；课程：数学；成绩：{80 + i % 20}")
        for i in range(300)
    ]
    group = SimpleNamespace(
        id=group_id, status=STRUCTURED_GROUP_CONFIRMED, kb_id=uuid.uuid4(),
        name="合成组", vector_count=0,
    )
    fake = FakeSession(group=group, group_records=records)
    store, emb = TrackingStore(), TrackingEmbedding()
    svc = StructuredVectorService(db=fake, embedding=emb, store=store)
    total = await svc.vectorize_group(group_id)
    ok(total == 300 and store.count_by_doc(group_key) == 300, "组记录全量入库（键集分页）")
    chunk = max(int(settings.VECTORIZE_ENCODE_BATCH_SIZE or 0), 1)
    expected_group_sizes = []
    for i in range(0, 300, 100):
        for j in range(0, 100, chunk):
            expected_group_sizes.append(min(chunk, 100 - j))
    ok(
        store.write_sizes == expected_group_sizes,
        f"整组记录按 {chunk} 条一批独立写入（{len(expected_group_sizes)} 批）",
    )
    ok(sum(fake.select_sizes) == 300, "组分批读取无重复无遗漏")
    rows = [r for r in store.all_rows() if r.doc_id == group_key]
    ok({r.id for r in rows} == {structured_group_vector_id(r.id) for r in records}, "组向量 id 集合一致")
    ok(all(r.title_path == "合成组" and r.source_kind == "structured_group" for r in rows), "组来源标识正确")
    gstates = [s["vector_status"] for s in fake.status_log]
    ok(gstates[0] == "pending" and gstates[1] == "indexing" and gstates[-1] == "completed",
       "组状态机不变（pending→indexing→…→completed）")

    group2_id = uuid.uuid4()
    group2_key = structured_group_doc_id(group2_id)
    store2 = TrackingStore()
    store2.insert([old_row(f"sgr:x-{i}", group2_key) for i in range(4)])
    fake2 = FakeSession(group=SimpleNamespace(
        id=group2_id, status="stale", kb_id=uuid.uuid4(), name="失效组", vector_count=4,
    ))
    svc2 = StructuredVectorService(db=fake2, embedding=TrackingEmbedding(), store=store2)
    total2 = await svc2.vectorize_group(group2_id)
    ok(total2 == 0 and store2.count_by_doc(group2_key) == 0, "未确认组旧向量立即清掉")
    ok(fake2.status_log[-1]["vector_status"] == "pending" and "未确认" in fake2.status_log[-1]["vector_error"], "未确认组置 pending 并记录原因")


async def test_ordinary_paths_unchanged():
    print("[6] 普通文档 replace_doc 与 Milvus insert_batch 契约不变")
    store = InMemoryVectorStore()
    store.insert([old_row(i, "doc-1", f"old-{i}") for i in ("a", "b", "c")])
    new_b = old_row("b", "doc-1", "new-b")
    new_d = old_row("d", "doc-1", "new-d")
    n = store.replace_doc("doc-1", [new_b, new_d])
    ok(n == 2, "replace_doc 返回写入条数")
    ok({r.id for r in store.all_rows()} == {"b", "d"}, "replace_doc 同主键覆盖+清过期（InMemory 语义不变）")
    ok(next(r for r in store.all_rows() if r.id == "b").content == "new-b", "重叠 id 内容为新值")

    def broken_insert(rows):
        raise VectorStoreError("模拟失败")

    store.insert = broken_insert
    try:
        store.replace_doc("doc-1", [old_row("e", "doc-1")])
        raise AssertionError("replace_doc 应当抛出")
    except VectorStoreError:
        pass
    ok({r.id for r in store.all_rows()} == {"b", "d"}, "BUG-046：写入失败旧向量原样保留")

    class FakeClient:
        def __init__(self):
            self.sizes = []
            self.flushes = 0
            self.last = None

        def insert(self, collection_name, data):
            self.sizes.append(len(data))
            self.last = data
            return {"insert_count": len(data)}

        def flush(self, name):
            self.flushes += 1

    ms = MilvusStore(uri="http://127.0.0.1:1")
    client = FakeClient()
    ms._client = client
    ms._dynamic_enabled = True
    rows = [
        VectorRow(
            id=f"chunk-{i}", doc_id="normal-doc", kb_id="kb", chunk_index=i,
            content=f"文本{i}", page_num=None, title_path=None,
            dense_vector=[0.1] * 4, sparse_vector={1: 0.5},
            source_kind="structured", source_id=f"id-{i}", source_name="f.tsv",
        )
        for i in range(20)
    ]
    old_batch = settings.VECTORIZE_BATCH_SIZE
    settings.VECTORIZE_BATCH_SIZE = 8
    try:
        total = ms.insert_batch(rows, flush=False)
        ok(total == 20 and client.sizes == [8, 8, 4], "insert_batch 按 VECTORIZE_BATCH_SIZE 分片发送")
        ok(client.flushes == 0, "flush=False 不落盘（延迟落盘契约不变）")
        item = client.last[-1]
        ok(item["source_kind"] == "structured" and item["page_num"] == 0, "动态字段与缺省值约定不变")
        total = ms.insert_batch(rows[:3], flush=True)
        ok(total == 3 and client.flushes == 1, "flush=True 立即落盘（insert 单篇路径契约不变）")
    finally:
        settings.VECTORIZE_BATCH_SIZE = old_batch


async def main():
    old_read = settings.STRUCTURED_VECTOR_READ_BATCH
    settings.STRUCTURED_VECTOR_READ_BATCH = 100
    try:
        await test_document_batching()
        await test_mid_write_failure()
        await test_duplicate_pk_fallback()
        await test_empty_records()
        await test_group_batching()
        await test_ordinary_paths_unchanged()
    finally:
        settings.STRUCTURED_VECTOR_READ_BATCH = old_read
    print(f"\nALL PASS：{CHECKS} 项断言全部通过")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"\nFAILED: {exc}")
        sys.exit(1)