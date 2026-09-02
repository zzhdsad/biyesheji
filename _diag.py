"""诊断 pytest 里为什么 fake 没生效"""
import sys
sys.path.insert(0, "c:/bi ye she ji/backend")

# ====== 模拟 pytest 实际顺序 ======

# Phase 1: pytest 收集 — test_upload.py import app
print("Phase 1: import src.main (routes/__init__ 也被加载)")
from src.main import app
from src.core import deps as deps_mod
print(f"  deps.get_current_user id = {id(deps_mod.get_current_user)}")

# Phase 2: conftest 开始执行模块级代码
print("\nPhase 2: conftest 模块级 monkeypatch")
import uuid
from src.domain.models import User

original = deps_mod.get_current_user
print(f"  original id = {id(original)}")

async def _fake(*args, **kwargs):
    u = User(id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
             email="test@test.com", username="tester", hashed_password="", role="admin")
    return u

deps_mod.get_current_user = _fake
print(f"  现在 deps.get_current_user id = {id(deps_mod.get_current_user)}")
print(f"  是 fake? {deps_mod.get_current_user is _fake}")

# Phase 3: 看 protected_router 里 Depends.dependency 指向什么
print("\nPhase 3: 检查 protected_router.dependencies")
import src.api.routes as routes_mod
for i, dep in enumerate(routes_mod.protected_router.dependencies):
    dep_fn = dep.dependency
    print(f"  [{i}] dependency = {dep_fn}, id = {id(dep_fn)}")
    print(f"       是 original? {dep_fn is original}")
    print(f"       是 fake? {dep_fn is _fake}")

# Phase 4: 关键——TestClient 里的路由树已经构建好了吗？
print("\nPhase 4: 直接用 app 测试")
from fastapi.testclient import TestClient
with TestClient(app) as c:
    # 试 chat 端点（被 protected_router 保护）
    r = c.post("/api/v1/chat/ask", json={"kb_ids": ["xxx"], "query": "hi"})
    print(f"  POST /chat/ask => {r.status_code}")
    if r.status_code == 401:
        print(f"    401 详情: {r.json()}")
    elif r.status_code == 422:
        print(f"    422 说明 fake 生效了（参数校验先过了）")
    else:
        print(f"    其他: {r.text[:200]}")
