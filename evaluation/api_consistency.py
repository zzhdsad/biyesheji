"""前后端接口一致性诊断（只读）。

提取前端 src/services/api.ts 中所有请求路径，与后端 openapi.json 的注册路由比对，
输出：前端调用了但后端不存在的接口（运行时必然 404）。
"""

import json
import re
import sys
import urllib.request

API_TS = "/tmp/api.ts"
OPENAPI = "http://localhost:8000/openapi.json"

# 匹配 api.get('/xxx') / api.post<Type>(`/xxx/${id}`) 等
PATTERN = re.compile(r"""api\.(get|post|put|patch|delete)\s*(?:<[^>]*>)?\s*\(\s*[`'"]([^`'"]+)""")


def normalize(path: str) -> str:
    """去掉查询串与模板变量，统一成可比较的形式。"""
    path = path.split("?")[0]
    path = re.sub(r"\$\{[^}]+\}", ":p", path)
    return "/api/v1" + path if path.startswith("/") else path


def main() -> None:
    with open(API_TS, encoding="utf-8") as fh:
        src = fh.read()
    calls = [(m.group(1).upper(), normalize(m.group(2))) for m in PATTERN.finditer(src)]

    spec = json.load(urllib.request.urlopen(OPENAPI, timeout=30))
    registered: dict[str, set[str]] = {}
    for path, ops in spec["paths"].items():
        for method in ops:
            registered.setdefault(path, set()).add(method.upper())

    missing = []
    for method, path in calls:
        # 参数名无关，比较路径骨架
        matched = any(
            re.sub(r"\{[^}]+\}", "{p}", p) == re.sub(r":p", "{p}", path) for p in registered
        )
        if not matched:
            missing.append((method, path))

    print(f"前端调用点: {len(calls)}  后端注册接口: {sum(len(v) for v in registered.values())}")
    print(f"前后端不一致（前端调用了后端不存在的接口）: {len(missing)}")
    seen = set()
    for method, path in missing:
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        print(f"  [缺失] {method:6} {path}")
        # 提示同前缀的相近路由，便于定位是改名还是删除
        prefix = "/".join(path.split("/")[:4])
        near = [p for p in registered if p.startswith(prefix)]
        if near:
            print(f"           后端相近: {sorted(near)[:4]}")


if __name__ == "__main__":
    sys.exit(main())
