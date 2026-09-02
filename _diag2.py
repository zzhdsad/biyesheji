"""快速诊断：conftest 改了 Depends.dependency，为什么运行时还是 401？"""
import sys, uuid
sys.path.insert(0, "c:/bi ye she ji/backend")

# 先 import 所有模块（模拟 pytest 收集阶段）
from src.main import app
import src.api.routes as routes_mod
from src.core import deps as deps_mod
from src.domain.models import User

original = deps_mod.get_current_user
print(f"[初始] protected_router.dependencies[0].dependency is original? "
      f"{routes_mod.protected_router.dependencies[0].dependency is original}")

# conftest 模块级代码
async def _fake(*args, **kwargs):
    return User(id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                email="t@t.com", username="t", hashed_password="", role="admin")

deps_mod.get_current_user = _fake
for _dep in routes_mod.protected_router.dependencies:
    if hasattr(_dep, "dependency"):
        object.__setattr__(_dep, "dependency", _fake)

print(f"[fake后] protected_router.dependencies[0].dependency is fake? "
      f"{routes_mod.protected_router.dependencies[0].dependency is _fake}")

# 现在模拟 test_upload.py client fixture —— new TestClient
from fastapi.testclient import TestClient
print("\n=== TestClient 创建 ===")
with TestClient(app) as c:
    # TestClient 创建后，再看 Depends.dependency
    print(f"[TestClient内] protected_router.dependencies[0].dependency is fake? "
          f"{routes_mod.protected_router.dependencies[0].dependency is _fake}")
    
    # 试请求
    r = c.get("/api/v1/kb")
    print(f"\nGET /api/v1/kb => {r.status_code}")
    if r.status_code == 401:
        print(f"  401: {r.json()}")
