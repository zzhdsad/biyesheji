"""健康检查接口测试。"""

from fastapi.testclient import TestClient

from src.main import app


def test_health_returns_200_with_components() -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "components" in data
    for name in ("postgres", "redis"):
        assert data["components"][name] in ("ok", "unavailable")
    # BUG-029：status 如实反映依赖状态（旧实现恒为 ok，组件全挂也谎报健康）
    all_ok = all(v == "ok" for v in data["components"].values())
    assert data["status"] == ("ok" if all_ok else "degraded")


def test_health_does_not_expose_env_details() -> None:
    """免鉴权端点不得泄露版本/环境等系统信息（BUG-029）。"""
    client = TestClient(app)
    data = client.get("/health").json()
    assert "version" not in data
    assert "env" not in data


def test_health_reports_degraded_when_redis_unavailable(monkeypatch) -> None:
    """Redis 不可用时 status 必须为 degraded，而不是 ok（BUG-029）。"""
    import redis.asyncio as aioredis

    def _boom(*_args, **_kwargs):
        raise RuntimeError("Redis 不可用（测试注入）")

    monkeypatch.setattr(aioredis, "from_url", _boom)

    data = TestClient(app).get("/health").json()
    assert data["components"]["redis"] == "unavailable"
    assert data["status"] == "degraded"


def test_health_closes_redis_client_even_when_ping_fails(monkeypatch) -> None:
    """ping 失败也必须关闭 Redis 客户端（BUG-029：旧实现跳过 aclose 泄漏连接）。"""
    import redis.asyncio as aioredis

    closed = {"count": 0}

    class _FakeClient:
        async def ping(self):
            raise RuntimeError("ping 失败（测试注入）")

        async def aclose(self):
            closed["count"] += 1

    monkeypatch.setattr(aioredis, "from_url", lambda *_a, **_k: _FakeClient())

    data = TestClient(app).get("/health").json()
    assert data["components"]["redis"] == "unavailable"
    assert closed["count"] == 1


def test_docs_accessible() -> None:
    """Swagger 文档可访问（交付标准）。"""
    client = TestClient(app)
    resp = client.get("/docs")
    assert resp.status_code == 200
