"""GET /documents 可选分页的单元测试（不依赖 PostgreSQL）。

集成分页用例在 test_list_pagination.py；本文件只验证端点构造的
`X-Total-Count` 与 `limit` 截断逻辑，避免数据库不可用时完全没有覆盖。
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.requests import Request
from starlette.responses import Response

from src.api.routes import documents as documents_route


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


@pytest.mark.anyio
async def test_list_documents_sets_total_header_and_limit(monkeypatch):
    kb_id = uuid.uuid4()
    request = Request(
        {"type": "http", "method": "GET", "path": "/api/v1/documents", "headers": []}
    )
    request.state.user = MagicMock()

    monkeypatch.setattr(
        documents_route,
        "get_accessible_kb_ids",
        AsyncMock(return_value={kb_id}),
    )
    db = MagicMock()
    db.scalar = AsyncMock(return_value=2)
    db.scalars = AsyncMock(return_value=_Rows([object()]))
    response = Response()

    rows = await documents_route.list_documents(
        request=request,
        response=response,
        kb_id=kb_id,
        limit=1,
        offset=0,
        db=db,
    )

    assert len(rows) == 1
    assert response.headers["X-Total-Count"] == "2"


@pytest.mark.anyio
async def test_list_documents_forbidden_kb_returns_empty_with_zero_total(monkeypatch):
    request = Request(
        {"type": "http", "method": "GET", "path": "/api/v1/documents", "headers": []}
    )
    request.state.user = MagicMock()
    monkeypatch.setattr(
        documents_route,
        "get_accessible_kb_ids",
        AsyncMock(return_value=set()),
    )
    response = Response()

    rows = await documents_route.list_documents(
        request=request,
        response=response,
        kb_id=uuid.uuid4(),
        limit=1,
        offset=0,
        db=MagicMock(),
    )

    assert rows == []
    assert response.headers["X-Total-Count"] == "0"
