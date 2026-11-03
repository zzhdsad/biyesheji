"""导入进度上报单元测试（进程内兜底，不依赖 Redis）。"""

import pytest

from src.application import import_progress as ip


@pytest.fixture(autouse=True)
def _memory_only(monkeypatch):
    """强制走进程内存储：Redis 不可用时行为应与 Redis 等价。"""
    ip.reset_progress_memory()
    monkeypatch.setattr(ip, "_get_client", lambda: None)
    yield
    ip.reset_progress_memory()


def test_valid_session_id():
    assert ip.valid_session_id("abc-123_XYZ")
    assert not ip.valid_session_id("")
    assert not ip.valid_session_id("../etc/passwd")
    assert not ip.valid_session_id("a" * 65)
    assert not ip.valid_session_id("中文会话")


@pytest.mark.anyio
async def test_update_and_read_roundtrip():
    progress = ip.ImportProgress("sess-1")
    await progress.update(phase="write", percent=12.34, message="写入中", force=True)
    snap = await ip.get_progress("sess-1")
    assert snap is not None
    assert snap["phase"] == "write"
    assert snap["percent"] == 12.3  # 保留一位小数
    assert snap["message"] == "写入中"
    assert snap["done"] is False
    assert snap["failed"] is False


@pytest.mark.anyio
async def test_update_is_throttled_without_force():
    progress = ip.ImportProgress("sess-2")
    await progress.update(phase="write", percent=10, message="10", force=True)
    # 同阶段、增幅 < 1% 且间隔 < 250ms：应被节流丢弃
    await progress.update(phase="write", percent=10.4, message="10.4")
    snap = await ip.get_progress("sess-2")
    assert snap["percent"] == 10
    assert snap["message"] == "10"


@pytest.mark.anyio
async def test_finish_carries_result():
    progress = ip.ImportProgress("sess-3")
    await progress.update(phase="kg", percent=99, message="图谱", force=True)
    result = {"job_id": "job-1", "records": 10}
    await progress.finish(result)
    snap = await ip.get_progress("sess-3")
    assert snap["done"] is True
    assert snap["phase"] == "done"
    assert snap["percent"] == 100
    assert snap["result"] == result


@pytest.mark.anyio
async def test_fail_marks_error_and_truncates_message():
    progress = ip.ImportProgress("sess-4")
    await progress.fail("x" * 1000)
    snap = await ip.get_progress("sess-4")
    assert snap["failed"] is True
    assert snap["done"] is False
    assert len(snap["message"]) == 500


@pytest.mark.anyio
async def test_unknown_or_invalid_session_returns_none():
    assert await ip.get_progress("missing") is None
    assert await ip.get_progress("../bad") is None
