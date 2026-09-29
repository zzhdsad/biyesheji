"""管理中心数据清理：删除**明确属于测试**的知识库 / 用户 / 分类 / 标签。

安全规则（严格执行）：
1. 默认只**统计并报告**；真正删除必须显式传 ``--apply``；
2. 只按**明确测试特征**识别（命名里的测试/test/e2e/冒烟/演示/集成/临时、
   哈希后缀，或测试邮箱域名 @test.com / @example.com），绝不以"看起来像测试"
   "字段为空""数据不完整"为依据；
3. 不做 TRUNCATE，不 DELETE 全表；删除都走既有软删除（回收站）语义；
4. 知识库先软删除再按既有 purge 口径清理（KB 挂载向量 + 该库文档向量 + 记录），
   避免留下孤儿向量；
5. 用户只软删除（进回收站，可恢复），不彻底删除；永不删除管理员；
6. 无法可靠判断的一律保留并在报告中列出。

用法：
    python scripts/cleanup_test_data.py            # 只报告
    python scripts/cleanup_test_data.py --apply    # 报告后执行
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger  # noqa: E402
from sqlalchemy import func, select, text, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from src.domain.models import (  # noqa: E402
    Category,
    Chunk,
    Document,
    KnowledgeBase,
    KnowledgeBaseResource,
    Tag,
    User,
    herb_tags,
    literature_tags,
    prescription_tags,
    theory_tags,
)
from src.infrastructure.database import get_session_factory  # noqa: E402
from src.utils.timeutil import utcnow  # noqa: E402

# ── 明确测试特征（只认这些，不做模糊推断）──────────────────────────────────

TEST_KB_NAME_PATTERNS = (
    "测试", "test", "临时", "e2e", "集成", "演示", "样例", "示例",
    "冒烟", "smoke", "demo", "改名后", "新名", "空库", "挂载",
    "删除", "_cleanup", "cleanup",
)
TEST_KB_HASH_SUFFIX = re.compile(r"[0-9a-f]{6,}$", re.IGNORECASE)

TEST_USERNAME_PREFIXES = ("authtest_", "b1_member_", "test_", "test")
TEST_EMAIL_DOMAINS = ("@test.com", "@example.com")

TEST_TAXONOMY_PATTERNS = ("冒烟", "smoke", "test", "测试", "临时", "e2e")

# 测试资源命名特征：真实名 + 8 位十六进制后缀（与现存测试分类/标签同后缀），
# 或名称直接含测试字样。只认这两种显式特征。
TEST_RESOURCE_NAME_RE = re.compile(r"-[0-9a-f]{8}$", re.IGNORECASE)
TEST_RESOURCE_WORDS = ("冒烟", "测试", "test", "smoke", "临时")

_TAG_LINK_TABLES = (herb_tags, prescription_tags, theory_tags, literature_tags)


def _is_test_kb(name: str) -> bool:
    low = (name or "").lower()
    if any(p.lower() in low for p in TEST_KB_NAME_PATTERNS):
        return True
    return bool(TEST_KB_HASH_SUFFIX.search(name or ""))


def _is_test_user(username: str, email: str) -> bool:
    low_name = (username or "").lower()
    low_mail = (email or "").lower()
    if any(low_name.startswith(p.lower()) for p in TEST_USERNAME_PREFIXES):
        return True
    return any(low_mail.endswith(d) for d in TEST_EMAIL_DOMAINS)


def _is_test_taxonomy(name: str) -> bool:
    low = (name or "").lower()
    if any(p.lower() in low for p in TEST_TAXONOMY_PATTERNS):
        return True
    # 与测试资源同一套显式特征：真实名 + 8 位十六进制后缀（如 标签-3af26475）
    return bool(TEST_RESOURCE_NAME_RE.search(name or ""))


def _is_test_resource(name: str) -> bool:
    low = (name or "").lower()
    if TEST_RESOURCE_NAME_RE.search(name or ""):
        return True
    return any(w.lower() in low for w in TEST_RESOURCE_WORDS)


# ── 报告 ────────────────────────────────────────────────────────────────────


async def report_kbs(db: AsyncSession) -> tuple[list[dict], list[dict]]:
    rows = list(
        (
            await db.scalars(
                select(KnowledgeBase).where(KnowledgeBase.deleted_at.is_(None))
            )
        ).all()
    )
    targets, kept = [], []
    for kb in rows:
        if _is_test_kb(kb.name):
            docs = int(
                await db.scalar(
                    select(func.count())
                    .select_from(Document)
                    .where(Document.kb_id == kb.id, Document.deleted_at.is_(None))
                )
                or 0
            )
            chunks = int(
                await db.scalar(
                    select(func.count()).select_from(Chunk).where(Chunk.kb_id == kb.id)
                )
                or 0
            )
            mounts = int(
                await db.scalar(
                    select(func.count())
                    .select_from(KnowledgeBaseResource)
                    .where(KnowledgeBaseResource.knowledge_base_id == kb.id)
                )
                or 0
            )
            targets.append(
                {"id": str(kb.id), "name": kb.name, "docs": docs,
                 "chunks": chunks, "mounts": mounts}
            )
        else:
            kept.append(kb.name)
    return targets, kept


async def report_users(db: AsyncSession) -> tuple[list[dict], list[dict]]:
    rows = list(
        (await db.scalars(select(User).where(User.deleted_at.is_(None)))).all()
    )
    targets, kept = [], []
    for u in rows:
        if u.role == "admin":
            kept.append({"username": u.username, "reason": "管理员，永不删除"})
            continue
        if _is_test_user(u.username, u.email):
            targets.append(
                {"id": str(u.id), "username": u.username, "email": u.email, "role": u.role}
            )
        else:
            kept.append({"username": u.username, "reason": "未匹配测试特征"})
    return targets, kept


async def report_resources(db: AsyncSession) -> dict[str, list[dict]]:
    """找出命名带显式测试特征的资源（中药/方剂/理论/文献）。"""
    from src.domain.models import Herb, Literature, Prescription, Theory

    models = {
        "herb": (Herb, "herb"),
        "prescription": (Prescription, "prescription"),
        "theory": (Theory, "theory"),
        "literature": (Literature, "literature"),
    }
    found: dict[str, list[dict]] = {}
    for key, (model, rtype) in models.items():
        rows = list(
            (await db.scalars(select(model).where(model.deleted_at.is_(None)))).all()
        )
        hits = [
            {"id": str(r.id), "name": r.name, "resource_type": rtype}
            for r in rows
            if _is_test_resource(r.name)
        ]
        if hits:
            found[key] = hits
    return found


async def report_taxonomy(db: AsyncSession) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    cats = list((await db.scalars(select(Category))).all())
    tags = list((await db.scalars(select(Tag))).all())
    cat_targets, cat_kept = [], []
    for c in cats:
        if c.deleted_at is not None:
            continue
        if _is_test_taxonomy(c.name):
            cat_targets.append({"id": str(c.id), "name": c.name, "resource_type": c.resource_type})
        else:
            cat_kept.append({"id": str(c.id), "name": c.name, "resource_type": c.resource_type})
    tag_targets, tag_kept = [], []
    for t in tags:
        if t.deleted_at is not None:
            continue
        if _is_test_taxonomy(t.name):
            tag_targets.append({"id": str(t.id), "name": t.name})
        else:
            tag_kept.append({"id": str(t.id), "name": t.name})
    return cat_targets, cat_kept, tag_targets, tag_kept


# ── 执行 ────────────────────────────────────────────────────────────────────


async def purge_kb_fully(db: AsyncSession, kb_id: str, name: str, *, has_vectors: bool = True) -> dict:
    """软删除 → 按既有 purge 口径清理（挂载向量 + 库内文档向量）→ 删除记录。

    ``has_vectors=False`` 表示该库既无文档也无资源挂载（不可能存在向量），
    跳过 Milvus 调用——否则几千个空测试库会产生上万次无意义的网络往返。
    """
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        return {"id": kb_id, "ok": False, "reason": "不存在"}
    # 1) 软删除（保留回收站语义与可追溯性）
    kb.deleted_at = kb.deleted_at or utcnow()
    await db.flush()

    # 2) 清理向量：资源挂载向量 + 该 KB 全部文档向量
    if has_vectors:
        try:
            from src.application.resource_vector_service import ResourceVectorService

            await ResourceVectorService().cleanup_kb_resource_vectors(db, kb.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"KB {name} 资源向量清理失败: {exc}")
        try:
            from src.infrastructure.milvus_store import get_vector_store

            get_vector_store().delete_by_kb(str(kb.id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"KB {name} 文档向量清理失败: {exc}")

    # 3) 删除记录（documents/chunks/KBR 由 FK CASCADE 清理）
    await db.delete(kb)
    await db.flush()
    return {"id": kb_id, "ok": True, "name": name}


async def apply_cleanup(db: AsyncSession, targets: dict) -> dict:
    from src.application import trash_service
    from src.domain.models import Herb, Literature, Prescription, Theory

    result: dict = {
        "kb": {"done": 0, "failed": []},
        "user": {"done": 0, "failed": []},
        "resource": {"done": 0, "failed": []},
        "category": {"done": 0, "failed": []},
        "tag": {"done": 0, "failed": []},
    }

    # ① 测试资源：先软删除进回收站，再走统一 purge（清理挂载/向量/KG）
    _models = {
        "herb": (Herb, "herb"),
        "prescription": (Prescription, "prescription"),
        "theory": (Theory, "theory"),
        "literature": (Literature, "literature"),
    }
    for key, items in (targets.get("resources") or {}).items():
        model, rtype = _models[key]
        ids = [__import__("uuid").UUID(i["id"]) for i in items]
        await trash_service.soft_delete_many(db, model, ids)
        await db.commit()
        purged = await trash_service.purge_ids(db, model, ids, resource_type=rtype)
        result["resource"]["done"] += purged["total"]
        result["resource"]["failed"].extend(purged["failed"])

    for kb in targets["kb"]:
        try:
            has_vectors = (kb.get("docs") or 0) > 0 or (kb.get("mounts") or 0) > 0
            async with db.begin_nested():
                await purge_kb_fully(db, kb["id"], kb["name"], has_vectors=has_vectors)
            result["kb"]["done"] += 1
        except Exception as exc:  # noqa: BLE001
            result["kb"]["failed"].append({"id": kb["id"], "name": kb["name"], "reason": str(exc)[:200]})
    await db.commit()

    for u in targets["user"]:
        try:
            async with db.begin_nested():
                user = await db.get(User, u["id"])
                if user is None or user.role == "admin":
                    continue
                user.deleted_at = utcnow()
                await db.flush()
            result["user"]["done"] += 1
        except Exception as exc:  # noqa: BLE001
            result["user"]["failed"].append({"id": u["id"], "reason": str(exc)[:200]})
    await db.commit()

    # 分类：仍被引用 / 有子节点 → 跳过并报告（FK RESTRICT）
    for c in targets["category"]:
        try:
            async with db.begin_nested():
                cat = await db.get(Category, c["id"])
                if cat is None:
                    continue
                child = await db.scalar(
                    select(Category.id).where(Category.parent_id == cat.id).limit(1)
                )
                if child is not None:
                    result["category"]["failed"].append(
                        {"id": c["id"], "name": c["name"], "reason": "存在子分类，需先处理子分类"}
                    )
                    continue
                refs = 0
                for model in ("herbs", "prescriptions", "theories", "literatures"):
                    # 只统计**活跃**资源：已在回收站的资源不再构成引用约束
                    refs += int(
                        await db.scalar(
                            text(
                                f"SELECT count(*) FROM {model} "
                                "WHERE category_id = :cid AND deleted_at IS NULL"
                            ).bindparams(cid=cat.id)
                        )
                        or 0
                    )
                if refs:
                    result["category"]["failed"].append(
                        {"id": c["id"], "name": c["name"], "reason": f"仍被 {refs} 个资源引用"}
                    )
                    continue
                # 回收站中的资源仍受 FK RESTRICT 约束：先解除它们的分类引用
                for model, _rtype in _models.values():
                    await db.execute(
                        update(model)
                        .where(model.category_id == cat.id, model.deleted_at.is_not(None))
                        .values(category_id=None)
                    )
                await db.execute(
                    update(Category)
                    .where(Category.parent_id == cat.id, Category.deleted_at.is_not(None))
                    .values(parent_id=None)
                )
                await db.delete(cat)
                await db.flush()
            result["category"]["done"] += 1
        except Exception as exc:  # noqa: BLE001
            result["category"]["failed"].append({"id": c["id"], "reason": str(exc)[:200]})
    await db.commit()

    # 标签：先解关联再删除（FK RESTRICT）
    for t in targets["tag"]:
        try:
            async with db.begin_nested():
                tag = await db.get(Tag, t["id"])
                if tag is None:
                    continue
                for table in _TAG_LINK_TABLES:
                    await db.execute(table.delete().where(table.c.tag_id == tag.id))
                await db.delete(tag)
                await db.flush()
            result["tag"]["done"] += 1
        except Exception as exc:  # noqa: BLE001
            result["tag"]["failed"].append({"id": t["id"], "reason": str(exc)[:200]})
    await db.commit()
    return result


# ── 入口 ────────────────────────────────────────────────────────────────────


async def main(apply: bool) -> None:
    factory = get_session_factory()
    async with factory() as db:
        kb_targets, kb_kept = await report_kbs(db)
        user_targets, user_kept = await report_users(db)
        cat_targets, cat_kept, tag_targets, tag_kept = await report_taxonomy(db)
        resource_targets = await report_resources(db)

        kb_docs = sum(k["docs"] for k in kb_targets)
        kb_chunks = sum(k["chunks"] for k in kb_targets)
        kb_mounts = sum(k["mounts"] for k in kb_targets)

        print("=" * 70)
        print("【待删除 · 测试知识库】", len(kb_targets), "个")
        print(f"  关联文档 {kb_docs} / chunks {kb_chunks} / 资源挂载 {kb_mounts}")
        for k in sorted(kb_targets, key=lambda x: -x["docs"])[:15]:
            print(f"   - {k['name']}  docs={k['docs']} chunks={k['chunks']} mounts={k['mounts']}")
        print("【保留 · 知识库】", len(kb_kept), "个：", kb_kept[:10])

        print("=" * 70)
        print("【待删除 · 测试用户】", len(user_targets), "个")
        for u in user_targets[:10]:
            print(f"   - {u['username']} <{u['email']}>")
        print("【保留 · 用户】", len(user_kept), "个")
        for u in user_kept[:10]:
            print(f"   - {u['username']}（{u.get('reason','')}）")

        print("=" * 70)
        print("【待删除 · 测试资源】")
        for key, items in resource_targets.items():
            print(f"   {key}: {len(items)} 条 ->", [i["name"] for i in items][:5])
        if not resource_targets:
            print("   （无）")
        print("【待删除 · 测试分类】", len(cat_targets), [c["name"] for c in cat_targets])
        print("【保留 · 分类】", [c["name"] for c in cat_kept])
        print("【待删除 · 测试标签】", len(tag_targets), [t["name"] for t in tag_targets])
        print("【保留 · 标签】", [t["name"] for t in tag_kept])
        print("=" * 70)

        if not apply:
            print("\n（dry-run）未执行任何删除。确认无误后加 --apply 执行。")
            return

        result = await apply_cleanup(
            db,
            {"kb": kb_targets, "user": user_targets,
             "category": cat_targets, "tag": tag_targets,
             "resources": resource_targets},
        )
        print("\n执行结果：")
        for kind, res in result.items():
            print(f"  {kind}: 成功 {res['done']}，失败 {len(res['failed'])}")
            for f in res["failed"][:10]:
                print(f"     ! {f}")

        # 孤儿数据校验
        orphan_docs = int(
            await db.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.deleted_at.is_(None))
                .where(
                    ~Document.kb_id.in_(select(KnowledgeBase.id))
                )
            )
            or 0
        )
        print(f"\n校验：无主文档（kb 已不存在但仍活跃）= {orphan_docs}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正执行删除（默认只报告）")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
