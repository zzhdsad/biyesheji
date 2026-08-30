"""健康检查接口测试。"""

from fastapi.testclient import TestClient

from src.main import app


def test_health_returns_ok() -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "components" in data


def test_docs_accessible() -> None:
    """Swagger 文档可访问（交付标准）。"""
    client = TestClient(app)
    resp = client.get("/docs")
    assert resp.status_code == 200
