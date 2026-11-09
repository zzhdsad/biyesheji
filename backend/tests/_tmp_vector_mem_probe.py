"""临时探针（用完即删）：对比外层编码批次 64 vs 256 的耗时、RSS 峰值与进度粒度。

播种一个临时结构化文件（dev 库），调用真实 StructuredVectorService.vectorize_document
（真实 BGE-M3 + 真实 Milvus）。两次运行使用同一批打乱顺序的文本，运行前清掉上一轮
向量，避免 Milvus「同主键追加」触发清旧写新的重放（否则耗时不可比）。
"""

from __future__ import annotations

import asyncio
import gc
import random
import sys
import threading
import time
import uuid

import psutil
from sqlalchemy import delete, select

from src.application.structured_vector_service import (
    StructuredVectorService,
    structured_doc_id,
)
from src.core.config import settings
from src.core.file_spaces import STRUCTURED_SPACE
from src.domain.models import (
    STRUCTURED_PARSE_SUCCESS,
    Document,
    KnowledgeBase,
    StructuredParse,
    StructuredRecord,
    User,
)
from src.infrastructure.database import get_session_factory
from src.infrastructure.embedding import get_embedding
from src.infrastructure.milvus_store import get_vector_store

N = int(sys.argv[2]) if len(sys.argv) > 2 else 270
CHUNKS = [int(x) for x in sys.argv[1].split(",")] if len(sys.argv) > 1 else [64, 256]
# (占比, 目标字数)：接近真实文件「短行多、长行少」的分布
MIX = ((0.60, 40), (0.30, 80), (0.10, 150))


def make_texts() -> list[str]:
    texts: list[str] = []
    for i in range(N):
        pos = i / N
        acc = 0.0
        target = MIX[-1][1]
        for ratio, length in MIX:
            acc += ratio
            if pos < acc:
                target = length
                break
        base = f"名称：测试药材{i}；性味：甘，微温；功效：补气健脾；归经：脾、肺经；"
        texts.append((base * 40)[:target])
    random.Random(7).shuffle(texts)  # 打乱顺序：每批都混有不同长度
    return texts


class RssSampler:
    """后台按 0.1s 采样进程 RSS 峰值。"""

    def __init__(self) -> None:
        self.peak = 0
        self._stop = threading.Event()
        self._proc = psutil.Process()

    def start(self) -> "RssSampler":
        self.peak = self._proc.memory_info().rss
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            rss = self._proc.memory_info().rss
            if rss > self.peak:
                self.peak = rss
            time.sleep(0.1)

    def stop(self) -> int:
        self._stop.set()
        self._thread.join(timeout=1)
        return self.peak


async def seed(db) -> tuple[uuid.UUID, uuid.UUID]:
    owner = await db.scalar(select(User).limit(1))
    kb = KnowledgeBase(
        name=f"tmp-mem-probe-{uuid.uuid4().hex[:6]}",
        description="",
        visibility="private",
        owner_id=owner.id,
    )
    db.add(kb)
    await db.flush()
    doc = Document(
        kb_id=kb.id,
        file_name="probe.tsv",
        file_type="tsv",
        file_size=1,
        storage_path="",
        parse_status="success",
        space=STRUCTURED_SPACE,
    )
    db.add(doc)
    await db.flush()
    db.add(
        StructuredParse(
            doc_id=doc.id,
            kb_id=kb.id,
            status=STRUCTURED_PARSE_SUCCESS,
            detected_format="tsv",
            record_count=N,
            read_count=N,
        )
    )
    for i, text in enumerate(make_texts()):
        db.add(
            StructuredRecord(
                doc_id=doc.id,
                kb_id=kb.id,
                seq_no=i,
                row_no=i + 1,
                knowledge_text=text,
                payload={"i": i},
            )
        )
    await db.commit()
    return kb.id, doc.id


async def cleanup(db, kb_id: uuid.UUID | None, doc_id: uuid.UUID | None) -> None:
    store = get_vector_store()
    if doc_id is not None:
        await asyncio.to_thread(store.delete_by_doc, structured_doc_id(doc_id))
        await asyncio.to_thread(store.flush)
        await db.execute(delete(Document).where(Document.id == doc_id))
    if kb_id is not None:
        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    await db.commit()


async def run_once(db, doc_id: uuid.UUID, chunk: int, emb) -> dict:
    settings.VECTORIZE_ENCODE_BATCH_SIZE = chunk
    store = get_vector_store()
    doc_key = structured_doc_id(doc_id)
    # 每轮开始前清空该文档向量：避免第二次运行因「同主键追加」触发回退重放
    await asyncio.to_thread(store.delete_by_doc, doc_key)
    await asyncio.to_thread(store.flush)

    svc = StructuredVectorService(db, embedding=emb)
    progress: list[int] = []
    orig_status = svc._set_parse_status

    async def wrapped_status(doc_pk, status, count, error, **kw):
        if status == "indexing" and int(count or 0) > 0:
            progress.append(int(count))
        return await orig_status(doc_pk, status, count, error, **kw)

    svc._set_parse_status = wrapped_status

    calls: list[int] = []
    orig_encode = emb.encode

    def counted_encode(texts):
        calls.append(len(texts))
        return orig_encode(texts)

    emb.encode = counted_encode
    sampler = RssSampler().start()
    started = time.time()
    total = await svc.vectorize_document(doc_id)
    elapsed = time.time() - started
    peak = sampler.stop()
    emb.encode = orig_encode
    svc._set_parse_status = orig_status
    stored = await asyncio.to_thread(store.count_by_doc, doc_key)
    return {
        "chunk": chunk,
        "total": total,
        "stored": stored,
        "elapsed": elapsed,
        "peak": peak,
        "progress": progress,
        "calls": calls,
    }


async def main() -> None:
    factory = get_session_factory()
    async with factory() as db:
        # 清掉上一轮被中止时可能残留的临时库/文档/向量
        stale_kbs = (
            await db.scalars(
                select(KnowledgeBase).where(KnowledgeBase.name.like("tmp-mem-probe-%"))
            )
        ).all()
        for kb in stale_kbs:
            docs = (
                await db.scalars(select(Document).where(Document.kb_id == kb.id))
            ).all()
            for d in docs:
                await cleanup(db, None, d.id)
            await cleanup(db, kb.id, None)

        kb_id, doc_id = await seed(db)
        emb = get_embedding()
        emb.encode(["名称：预热；药性：甘平。"])  # 模型权重/首次前向不计入任何一组
        print(f"baseline_rss={psutil.Process().memory_info().rss / 1e6:.0f}MB")
        results = []
        try:
            for chunk in CHUNKS:
                r = await run_once(db, doc_id, chunk, emb)
                results.append(r)
                print(
                    f"chunk={r['chunk']} total={r['total']} stored={r['stored']} "
                    f"elapsed={r['elapsed']:.1f}s "
                    f"per_row={r['elapsed'] / max(r['total'], 1):.3f}s "
                    f"peak={r['peak'] / 1e6:.0f}MB"
                )
                print(f"  encode_calls={r['calls']}")
                print(f"  progress={r['progress']}")
                gc.collect()
            base, new = results[0], results[1]
            print(
                f"diff={(base['elapsed'] - new['elapsed']) / base['elapsed'] * 100:.1f}% "
                f"peak_delta={(new['peak'] - base['peak']) / 1e6:.0f}MB"
            )
        finally:
            await cleanup(db, kb_id, doc_id)
            print("cleanup_done")


if __name__ == "__main__":
    asyncio.run(main())
