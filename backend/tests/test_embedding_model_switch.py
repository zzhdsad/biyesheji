"""BGE-M3 依赖初始化 / 模型切换存量识别 / 批量重新向量化 测试。

覆盖产品问题："切换到 BGE-M3 后必须用户自己 pip install + 逐篇重新向量化"：

1. 运行状态：依赖与模型随镜像内置，无安装态；重建任务进行中为 revectorizing
2. 存量向量识别：未标注（legacy）不误判；显式旧模型被识别为待重建
3. 批量重新向量化：总数 / 完成 / 失败 / 进度 / 后台异步
4. 失败与重试：失败项可精确重试，不破坏已有数据
5. 取消：进行中任务可取消，当前文档处理完即停，已完成部分不回滚
6. 新旧模型不混检：旧模型文档必须从检索结果中剔除

conftest 已把 embedding 固定为 mock（`_force_mock_model_config`），
因此本文件的"当前模型"统一为 `mock:BAAI/bge-m3`。
"""

import asyncio
import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings
from src.domain.models import Document, RevectorizeJob
from src.infrastructure.milvus_store import VectorStoreError
from tests.test_chat import _upload_doc
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE

pytestmark = pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")

# conftest `_force_mock_model_config` 把生效配置固定为 mock 后端、
# embedding_model="mock"，因此当前模型标识为 `mock:mock`。
CURRENT_KEY = "mock:mock"
LEGACY_KEY = "mock:legacy-old-model"

# 等待后台任务完成的轮询上限（mock 向量化很快）
JOB_TIMEOUT_SECONDS = 90


def _run(coro):
    """在独立事件循环里执行一段异步 DB 操作（测试内不依赖 pytest-asyncio）。"""

    async def _main():
        engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                return await coro(session)
        finally:
            await engine.dispose()

    return asyncio.run(_main())


def _set_vector_model(doc_id: str, value: str | None) -> None:
    async def _update(session):
        await session.execute(
            update(Document).where(Document.id == uuid.UUID(doc_id)).values(vector_model=value)
        )
        await session.commit()

    _run(_update)


def _get_vector_model(doc_id: str):
    async def _read(session):
        doc = await session.get(Document, uuid.UUID(doc_id))
        return doc.vector_model if doc else None

    return _run(_read)


@pytest.fixture(autouse=True)
def _align_settings_config(monkeypatch):
    """让设置路由与 conftest 的 mock 配置保持一致。

    settings.py 通过 `from ... import get_effective_config_cached` 绑定引用，
    conftest 只 patch 了 mcs / rag_service / indexing_service（后者为本批次补充）。
    这里按同一口径 patch 设置路由，使"状态接口的当前模型"与"向量化实际使用的
    模型"一致，测试才具备确定性（产品代码本身不受影响）。
    """
    import src.api.routes.settings as settings_mod

    async def _mock_cached(db):
        return {
            "llm_provider": "mock",
            "llm_base_url": "",
            "llm_model": "mock",
            "llm_api_key": "",
            "embedding_backend": "mock",
            "embedding_model": "mock",
            "embedding_device": "cpu",
            "rerank_backend": "mock",
            "rerank_model": "mock",
            "rerank_device": "cpu",
            "hyde_enabled": settings.HYDE_ENABLED,
            "hyde_backend": "mock",
            "hyde_model": "mock",
            "hyde_base_url": "",
        }

    monkeypatch.setattr(settings_mod, "get_effective_config_cached", _mock_cached)


@pytest.fixture(autouse=True)
def _clear_unfinished_jobs():
    """清理历史遗留的未完成任务（避免上一个测试进程被中断后阻塞新任务）。"""

    async def _reap(session):
        from sqlalchemy import update as _update

        await session.execute(
            _update(RevectorizeJob)
            .where(RevectorizeJob.status.in_(["pending", "running"]))
            .values(status="failed", error_message="测试清理：上一轮残留任务")
        )
        await session.commit()

    _run(_reap)
    yield


def _embedding_status(client) -> dict:
    resp = client.get("/api/v1/settings/model/embedding/status")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _wait_job_done(client, timeout: int = JOB_TIMEOUT_SECONDS) -> dict:
    """轮询批量任务直到结束（后台线程执行，HTTP 立即返回）。"""
    import time

    # 说明：cancelled 由取消接口立即置位，此时 worker 可能仍在收尾；
    # 需要 finished_at 的用例请在拿到结果后自行等待。

    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        resp = client.get("/api/v1/settings/model/revectorize/status")
        assert resp.status_code == 200, resp.text
        last = resp.json() or {}
        if last.get("status") in ("succeeded", "partial", "failed", "cancelled"):
            return last
        time.sleep(0.5)
    raise AssertionError(f"批量任务未在 {timeout}s 内结束：{last}")


# ── 1. Embedding 运行状态 ──────────────────────────────────────────────────


def test_status_ready_by_default(client):
    """模型与依赖随镜像内置：无重建任务时状态恒为 ready（无需用户安装）。"""
    data = _embedding_status(client)
    assert data["overall_status"] == "ready"
    assert "dependency" not in data, "不再暴露依赖安装状态"


def test_status_revectorizing_while_job_running(client):
    """存在进行中的重建任务时，状态为 revectorizing；任务结束后回到 ready。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id, "状态联动.txt")
    _set_vector_model(_upload_doc(client, kb_id, "状态联动2.txt"), LEGACY_KEY)

    resp = client.post("/api/v1/settings/model/revectorize", json={"kb_id": kb_id})
    if resp.status_code == 200:
        assert _embedding_status(client)["overall_status"] == "revectorizing"
        _wait_job_done(client)
    assert _embedding_status(client)["overall_status"] == "ready"


# ── 2. 存量向量识别 ──────────────────────────────────────────────────────────


def test_vectorized_document_is_stamped_with_model(client):
    """向量化成功后必须记录"该向量由哪个模型生成"。"""
    kb_id = _make_kb(client)
    doc_id = _upload_doc(client, kb_id, "模型标识.txt")

    assert _get_vector_model(doc_id) == CURRENT_KEY


def test_legacy_unlabeled_documents_are_not_stale(client):
    """未标注（本功能上线前入库）不判定为旧模型 → 升级不影响既有检索。

    用同一篇文档的"标注前 / 标注为 NULL 后"对比，避免受其它用例残留数据影响。
    """
    kb_id = _make_kb(client)
    doc_id = _upload_doc(client, kb_id, "legacy.txt")

    baseline = _embedding_status(client)["vectors"]["stale_documents"]
    _set_vector_model(doc_id, None)
    after = _embedding_status(client)["vectors"]["stale_documents"]

    assert _embedding_status(client)["vectors"]["current_model_key"] == CURRENT_KEY
    assert after == baseline, "未标注文档不得被判定为旧模型"


def test_mismatch_is_detected(client):
    """显式旧模型 → 进入待重建统计，且能列出旧模型标识。"""
    kb_id = _make_kb(client)
    doc_id = _upload_doc(client, kb_id, "旧模型.txt")

    baseline = _embedding_status(client)["vectors"]["stale_documents"]
    _set_vector_model(doc_id, LEGACY_KEY)
    data = _embedding_status(client)

    assert data["vectors"]["stale_documents"] == baseline + 1
    assert LEGACY_KEY in data["vectors"]["stale_models"]


# ── 3. 批量重新向量化 ────────────────────────────────────────────────────────


def test_batch_revectorize_updates_documents(client):
    """一键批量重新向量化：进度可见、完成后文档向量模型切换为当前模型。"""
    kb_id = _make_kb(client)
    doc_id = _upload_doc(client, kb_id, "批量重建.txt")
    _set_vector_model(doc_id, LEGACY_KEY)

    # 限定本用例的知识库，避免受其它用例残留文档影响
    resp = client.post("/api/v1/settings/model/revectorize", json={"kb_id": kb_id})
    assert resp.status_code == 200, resp.text
    job = resp.json()
    assert job["total"] == 1
    assert job["status"] == "pending"

    done = _wait_job_done(client)
    assert done["status"] == "succeeded", done
    assert done["processed"] == done["total"]
    assert done["succeeded"] == 1
    assert done["failed"] == 0
    assert done["percent"] == 100

    # 新向量写入成功后才更新标识（先写新后清旧，无空窗）
    assert _get_vector_model(doc_id) == CURRENT_KEY


def test_batch_revectorize_failure_and_retry(client, vector_store):
    """失败计入 failed 并保留失败项；修复后可精确重试（不重建已成功文档）。"""
    kb_id = _make_kb(client)
    doc_id = _upload_doc(client, kb_id, "失败重试.txt")
    _set_vector_model(doc_id, LEGACY_KEY)

    original_insert = vector_store.insert

    def boom(rows):  # noqa: ANN001 - 测试替身
        raise VectorStoreError("模拟 Milvus 不可用")

    vector_store.insert = boom
    try:
        resp = client.post("/api/v1/settings/model/revectorize", json={"kb_id": kb_id})
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["job_id"]

        done = _wait_job_done(client)
        assert done["status"] == "partial", done
        assert done["failed"] >= 1
        assert done["failed_doc_ids"], "必须记录失败文档以便重试"
        assert done["error_message"], "Milvus 不可用时必须给出明确原因"
        # 失败不得把文档标记成成功
        assert _get_vector_model(doc_id) == LEGACY_KEY
    finally:
        vector_store.insert = original_insert

    retry = client.post("/api/v1/settings/model/revectorize/retry", json={"job_id": job_id})
    assert retry.status_code == 200, retry.text
    assert retry.json()["total"] == 1, "重试只重建失败文档"

    done = _wait_job_done(client)
    assert done["status"] == "succeeded", done
    assert done["failed"] == 0
    assert _get_vector_model(doc_id) == CURRENT_KEY


def test_cancel_running_job_stops_processing(client, monkeypatch):
    """取消进行中的任务：当前文档处理完即停止，剩余文档保持旧向量不被改动。"""
    from src.application import revectorize_service

    class _SlowIndexingService:
        """替身：让每篇耗时可观测，保证取消请求落在"进行中"而非"已结束"。"""

        def __init__(self, db) -> None:  # noqa: ANN001 - 测试替身
            self.db = db

        async def run(self, doc_id) -> None:  # noqa: ANN001 - 测试替身
            await asyncio.sleep(1.0)

    monkeypatch.setattr(revectorize_service, "IndexingService", _SlowIndexingService)

    kb_id = _make_kb(client)
    first = _upload_doc(client, kb_id, "取消1.txt")
    last = _upload_doc(client, kb_id, "取消2.txt")
    _set_vector_model(first, LEGACY_KEY)
    _set_vector_model(last, LEGACY_KEY)

    resp = client.post("/api/v1/settings/model/revectorize", json={"kb_id": kb_id})
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    assert resp.json()["total"] == 2

    # 等任务真正进入 running（后台线程启动有延迟），确保验证的是"中途取消"
    deadline = time.time() + JOB_TIMEOUT_SECONDS
    while time.time() < deadline:
        snapshot = client.get("/api/v1/settings/model/revectorize/status").json() or {}
        if snapshot.get("status") == "running":
            break
        time.sleep(0.2)
    else:
        raise AssertionError("任务未进入 running 状态")

    cancel = client.post("/api/v1/settings/model/revectorize/cancel", json={"job_id": job_id})
    assert cancel.status_code == 200, cancel.text

    done = _wait_job_done(client)
    # cancelled 由取消接口立即置位，worker 还需处理完当前文档再收尾；等 finished_at 落地
    deadline = time.time() + JOB_TIMEOUT_SECONDS
    while done.get("finished_at") is None and time.time() < deadline:
        time.sleep(0.5)
        done = client.get("/api/v1/settings/model/revectorize/status").json()
    assert done["status"] == "cancelled", done
    assert done["finished_at"], "worker 收尾后必须写入 finished_at"
    # 已完成至少一篇（取消发生在处理中），且不得继续处理剩余文档
    assert 0 < done["processed"] < done["total"], done
    assert "已取消" in done["error_message"], done
    # 协作式取消不做回滚：未处理的文档仍是旧模型向量，可再次发起重建
    assert _get_vector_model(last) == LEGACY_KEY


def test_cancel_finished_job_rejects(client):
    """任务已结束时取消应被拒绝（400），不能把已完成任务改成 cancelled。"""
    kb_id = _make_kb(client)
    doc_id = _upload_doc(client, kb_id, "取消已结束.txt")
    _set_vector_model(doc_id, LEGACY_KEY)

    resp = client.post("/api/v1/settings/model/revectorize", json={"kb_id": kb_id})
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    assert _wait_job_done(client)["status"] == "succeeded"

    cancel = client.post("/api/v1/settings/model/revectorize/cancel", json={"job_id": job_id})
    assert cancel.status_code == 400, cancel.text


def test_revectorize_without_stale_documents_rejects(client):
    """没有旧模型向量时不应创建空任务（避免误导用户）。

    注意：必须**限定本用例新建的知识库**发起请求。若用全库（kb_id=None），
    只要库中残留任何旧模型文档就会真的创建后台任务；该任务在用例结束后仍会在
    后台线程运行，此时 conftest 的 mock 配置 patch 已撤销，线程会按 DB 真实
    配置加载 BGE-M3（2.3GB），把整个测试拖慢数分钟。
    """
    kb_id = _make_kb(client)
    doc_a = _upload_doc(client, kb_id, "无需重建.txt")
    doc_b = _upload_doc(client, kb_id, "无需重建2.txt")
    _set_vector_model(doc_a, CURRENT_KEY)
    _set_vector_model(doc_b, CURRENT_KEY)

    resp = client.post("/api/v1/settings/model/revectorize", json={"kb_id": kb_id})
    assert resp.status_code == 400, resp.text
    assert "没有需要重新向量化" in resp.json()["message"]


# ── 4. 新旧模型不混检 ────────────────────────────────────────────────────────


def test_stale_vectors_are_excluded_from_retrieval(client):
    """旧模型文档必须从检索结果中剔除，禁止新旧向量混检。"""
    from tests.test_chat import _ask

    kb_id = _make_kb(client)
    doc_id = _upload_doc(client, kb_id, "混检隔离.txt")

    baseline = _ask(client, "公司实行什么工时制度？", kb_id)
    assert baseline["citations"], "基线：正常文档应能召回"

    # 标记为其它模型生成 → 检索必须排除（否则相似度无意义）
    _set_vector_model(doc_id, LEGACY_KEY)
    after = _ask(client, "公司实行什么工时制度？", kb_id)
    assert after["citations"] == [], "旧模型向量不得参与检索（新旧模型不可混检）"

    # 重建后恢复可检索（限定本知识库）
    resp = client.post("/api/v1/settings/model/revectorize", json={"kb_id": kb_id})
    assert resp.status_code == 200, resp.text
    done = _wait_job_done(client)
    assert done["status"] == "succeeded", done
    restored = _ask(client, "公司实行什么工时制度？", kb_id)
    assert restored["citations"], "重新向量化后文档应恢复可检索"
