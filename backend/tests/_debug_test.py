"""pytest 模块级调试：看 conftest fake 有没有真的生效"""
import sys
print(f"\n[DEBUG conftest] 文件: {__file__}")
print(f"[DEBUG conftest] sys.path[0] = {sys.path[0]}")

from src.main import app
import src.api.routes as routes_mod
from src.core import deps as deps_mod

dep_fn = routes_mod.protected_router.dependencies[0].dependency
real_fn = deps_mod.get_current_user
print(f"[DEBUG conftest] app id = {id(app)}")
print(f"[DEBUG conftest] protected_router.deps[0].dependency = {dep_fn}")
print(f"[DEBUG conftest] 当前 deps.get_current_user = {real_fn}")
print(f"[DEBUG conftest] 两者相同? {dep_fn is real_fn}")


def test_diagnose():
    """确认运行时 fake 生效"""
    import src.api.routes as routes_mod
    from src.core import deps as deps_mod
    dep_fn = routes_mod.protected_router.dependencies[0].dependency
    real_fn = deps_mod.get_current_user
    print(f"\n[DEBUG test] protected_router.deps[0].dependency = {dep_fn}")
    print(f"[DEBUG test] 当前 deps.get_current_user = {real_fn}")
    print(f"[DEBUG test] 两者相同? {dep_fn is real_fn}")

    from fastapi.testclient import TestClient
    from src.main import app
    with TestClient(app) as c:
        r = c.get("/api/v1/kb")
        print(f"[DEBUG test] GET /api/v1/kb => {r.status_code}")
