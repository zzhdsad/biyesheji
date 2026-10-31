"""清理前的环境归属校验（仅用标准库，可复制进容器独立运行）。

只有「目标数据库 + 后端实际连接库 + Milvus 集合 + 上传目录挂载」全部
命中同一个已登记环境画像时才允许继续；任一字段缺失、未知或不一致都拒绝。

用法（由 reset-database.ps1 调用）：
    python env_guard.py --facts-b64 <base64(JSON)>
退出码：0 允许，3 拒绝。
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from dataclasses import dataclass

UPLOAD_CONTAINER_DIR = "/app/uploads"


@dataclass(frozen=True)
class EnvironmentProfile:
    db: str
    milvus_collection: str
    upload_dir_name: str
    requires_allow_main: bool = False


PROFILES: dict[str, EnvironmentProfile] = {
    "kp_dev": EnvironmentProfile(
        db="kp_dev",
        milvus_collection="document_chunks_dev",
        upload_dir_name="uploads_dev",
    ),
    "knowledge_platform": EnvironmentProfile(
        db="knowledge_platform",
        milvus_collection="document_chunks",
        upload_dir_name="uploads",
        requires_allow_main=True,
    ),
}


@dataclass(frozen=True)
class EnvironmentFacts:
    db: str = ""
    backend_db: str = ""
    milvus_collection: str = ""
    upload_source: str = ""
    upload_container_dir: str = ""
    allow_main: bool = False


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str
    profile: str | None = None
    environment: dict | None = None

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "profile": self.profile,
            "environment": self.environment or {},
        }


def _dir_name(path: str) -> str:
    cleaned = (path or "").strip().rstrip("\\/")
    if not cleaned:
        return ""
    return re.split(r"[\\/]", cleaned)[-1]


def check_environment(facts: EnvironmentFacts) -> GuardResult:
    db = (facts.db or "").strip()
    if not db:
        return GuardResult(False, "无法确认目标数据库")

    profile = PROFILES.get(db)
    if profile is None:
        return GuardResult(False, f"未登记的环境：{db}，拒绝清理")

    backend_db = (facts.backend_db or "").strip()
    if not backend_db:
        return GuardResult(False, "无法确认后端当前连接的数据库", profile=db)
    if backend_db != db:
        return GuardResult(
            False, f"后端当前连接 {backend_db}，与目标库 {db} 不一致", profile=db
        )

    collection = (facts.milvus_collection or "").strip()
    if not collection:
        return GuardResult(False, "无法确认 Milvus 集合归属", profile=db)
    if collection != profile.milvus_collection:
        return GuardResult(
            False,
            f"Milvus 集合 {collection} 不属于环境 {db}（期望 {profile.milvus_collection}）",
            profile=db,
        )

    if (facts.upload_container_dir or "").strip() != UPLOAD_CONTAINER_DIR:
        return GuardResult(
            False,
            f"上传目录挂载点异常：{facts.upload_container_dir or '未检测到'}",
            profile=db,
        )

    source = (facts.upload_source or "").strip()
    if not source:
        return GuardResult(
            False, "未检测到上传目录挂载，拒绝清理容器内临时路径", profile=db
        )

    name = _dir_name(source)
    if not name or name in {".", ".."}:
        return GuardResult(False, f"上传目录无法识别：{source}", profile=db)
    if name != profile.upload_dir_name:
        return GuardResult(
            False,
            f"上传目录 {source} 不属于环境 {db}（期望目录名 {profile.upload_dir_name}）",
            profile=db,
        )

    if profile.requires_allow_main and not facts.allow_main:
        return GuardResult(
            False, f"目标是主库 {db}，需要显式 -AllowMain 才允许", profile=db
        )

    return GuardResult(
        True,
        f"环境校验通过：{db}",
        profile=db,
        environment={
            "db": db,
            "milvus_collection": collection,
            "upload_source": source,
        },
    )


def facts_from_b64(raw: str) -> EnvironmentFacts:
    payload = json.loads(base64.b64decode(raw).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("facts 必须是 JSON 对象")
    return EnvironmentFacts(
        db=str(payload.get("db", "") or ""),
        backend_db=str(payload.get("backend_db", "") or ""),
        milvus_collection=str(payload.get("milvus_collection", "") or ""),
        upload_source=str(payload.get("upload_source", "") or ""),
        upload_container_dir=str(payload.get("upload_container_dir", "") or ""),
        allow_main=bool(payload.get("allow_main", False)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理前的环境归属校验")
    parser.add_argument("--facts-b64", required=True)
    args = parser.parse_args(argv)
    try:
        facts = facts_from_b64(args.facts_b64)
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都必须拒绝
        print(
            json.dumps(
                {"allowed": False, "reason": f"facts 无法解析：{exc}"}, ensure_ascii=False
            )
        )
        return 3
    result = check_environment(facts)
    print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0 if result.allowed else 3


if __name__ == "__main__":
    sys.exit(main())
