"""映射模板：保存 / 识别 / 复用 / 版本兼容（真实 PostgreSQL）。

指纹稳定性与版本迁移是模板功能的核心风险点：
- 指纹受列顺序、文件名影响 → 同一份数据换个顺序就匹配不上，模板形同虚设；
- 版本不兼容时若按猜测解释，会静默错配主键与关系，比直接失败危险得多。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.mapping_template_service import (
    CURRENT_CONFIG_VERSION,
    MappingTemplateService,
    TemplateConfig,
    TemplateError,
    dataset_fingerprint,
)
from src.core.config import settings
from src.domain.models import ImportMappingTemplate, KnowledgeBase, User


def _postgres_available() -> bool:
    async def _ping() -> None:
        engine = create_async_engine(settings.DATABASE_URL)
        try:
            async with engine.connect() as conn:
                await conn.execute(select(1))
        finally:
            await engine.dispose()

    try:
        asyncio.run(_ping())
        return True
    except Exception:
        return False


PG_AVAILABLE = _postgres_available()


def _new_session():
    engine = create_async_engine(settings.DATABASE_URL)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_kb(session, tag: str):
    user = User(
        email=f"{tag}@test.local",
        username=tag[:40],
        hashed_password="not-a-real-hash",
        role="admin",
        name="测试管理员",
        department="测试部",
        must_change_password=False,
    )
    session.add(user)
    await session.flush()
    kb = KnowledgeBase(name=f"KB-{tag}", owner_id=user.id, visibility="private")
    session.add(kb)
    await session.flush()
    return user, kb


# ── 指纹 ────────────────────────────────────────────────────────────────────


def test_fingerprint_ignores_column_order() -> None:
    a = dataset_fingerprint({"D1": ["术语id", "术语名称"]})
    b = dataset_fingerprint({"D1": ["术语名称", "术语id"]})
    assert a == b


def test_fingerprint_ignores_table_order_and_name_case() -> None:
    a = dataset_fingerprint({"D1": ["术语id"], "D2": ["中成药id"]})
    b = dataset_fingerprint({"D2": ["中成药id"], "D1": ["术语id"]})
    assert a == b
    assert dataset_fingerprint({"D1": ["术语id"]}) == dataset_fingerprint({"D1": ["术语ID"]})


def test_fingerprint_changes_when_schema_changes() -> None:
    base = dataset_fingerprint({"D1": ["术语id", "术语名称"]})
    assert base != dataset_fingerprint({"D1": ["术语id"]})  # 少一列
    assert base != dataset_fingerprint({"D1": ["术语id", "术语解释"]})  # 换一列
    assert base != dataset_fingerprint({"D1": ["术语id", "术语名称"], "D2": ["id"]})  # 多一张表


# ── 版本兼容 ────────────────────────────────────────────────────────────────


def test_v1_config_is_migrated_to_current() -> None:
    """v1 缺 relation_type，迁移后必须补默认值且关系不丢。"""
    v1 = {
        "version": 1,
        "tables": {"D1": {"table": "D1", "entity_type": "术语", "pk_field": "术语id"}},
        "relations": [
            {
                "child_table": "D2",
                "child_field": "术语id",
                "parent_table": "D1",
                "parent_field": "术语id",
            }
        ],
        "junctions": [],
    }
    config = TemplateConfig.from_dict(v1)

    assert config.version == CURRENT_CONFIG_VERSION
    assert config.tables["D1"].pk_field == "术语id"
    assert len(config.relations) == 1
    assert config.relations[0].relation_type == "related_to"


def test_future_version_is_rejected_not_guessed() -> None:
    future = {
        "version": CURRENT_CONFIG_VERSION + 1,
        "tables": {},
        "relations": [],
        "junctions": [],
    }
    with pytest.raises(TemplateError) as exc:
        TemplateConfig.from_dict(future)
    assert "高于当前支持" in str(exc.value)


def test_malformed_config_is_rejected() -> None:
    with pytest.raises(TemplateError):
        TemplateConfig.from_dict({"version": 2, "tables": {"D1": "not-a-dict"}})


def test_config_roundtrip() -> None:
    config = TemplateConfig(
        tables={"D1": __import__(
            "src.application.mapping_template_service", fromlist=["TableMapping"]
        ).TableMapping(table="D1", entity_type="术语", pk_field="术语id")},
        relations=[
            __import__(
                "src.application.mapping_template_service", fromlist=["RelationMapping"]
            ).RelationMapping("D2", "术语id", "D1", "术语id", "引用")
        ],
        junctions=[],
    )
    restored = TemplateConfig.from_dict(config.to_dict())
    assert restored.tables["D1"].pk_field == "术语id"
    assert restored.relations[0].relation_type == "引用"


# ── 真实库：保存 / 匹配 / 复用 ──────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_save_and_match_template_exact():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed_kb(session, f"tpl-{uuid.uuid4().hex[:8]}")
        tables = {"D1": ["术语id", "术语名称"], "D2": ["中成药id", "名称"]}
        svc = MappingTemplateService(session)
        try:
            await svc.save(
                kb_id=kb.id,
                name="D1D2 标准映射",
                fingerprint=dataset_fingerprint(tables),
                config={
                    "version": 2,
                    "tables": {
                        "D1": {"table": "D1", "entity_type": "术语", "pk_field": "术语id"},
                        "D2": {"table": "D2", "entity_type": "中成药", "pk_field": "中成药id"},
                    },
                    "relations": [],
                    "junctions": [],
                },
                user=user,
            )
            await session.commit()

            result = await svc.match(kb.id, tables)
            assert result.status == "exact"
            assert result.usable is True
            assert result.template is not None
            plan = MappingTemplateService.to_plan(
                TemplateConfig.from_dict(result.template.config)
            )
            assert plan["primary_keys"] == {"D1": "术语id", "D2": "中成药id"}
        finally:
            await session.execute(
                ImportMappingTemplate.__table__.delete().where(
                    ImportMappingTemplate.kb_id == kb.id
                )
            )
            await session.delete(kb)
            await session.commit()
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_match_reports_mismatch_when_field_dropped():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed_kb(session, f"tpl2-{uuid.uuid4().hex[:8]}")
        svc = MappingTemplateService(session)
        try:
            await svc.save(
                kb_id=kb.id,
                name="含主键映射",
                fingerprint=dataset_fingerprint({"D1": ["术语id"]}),
                config={
                    "version": 2,
                    "tables": {"D1": {"table": "D1", "entity_type": "术语", "pk_field": "术语id"}},
                    "relations": [],
                    "junctions": [],
                },
                user=user,
            )
            await session.commit()

            # 当前数据少了模板依赖的主键列 → 必须判定不匹配，而不是照套
            result = await svc.match(kb.id, {"D1": ["术语名称"]})
            assert result.status == "mismatch"
            assert result.usable is False
            assert result.missing_fields == {"D1": ["术语id"]}
        finally:
            await session.execute(
                ImportMappingTemplate.__table__.delete().where(
                    ImportMappingTemplate.kb_id == kb.id
                )
            )
            await session.delete(kb)
            await session.commit()
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_global_template_is_reusable_without_kb_scope():
    """全局模板（kb_id 为空）对任意知识库可见，避免每个库重复配置。"""
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed_kb(session, f"tpl3-{uuid.uuid4().hex[:8]}")
        tables = {"T": ["id", "name"]}
        svc = MappingTemplateService(session)
        try:
            await svc.save(
                kb_id=None,
                name="全局模板",
                fingerprint=dataset_fingerprint(tables),
                config={
                    "version": 2,
                    "tables": {"T": {"table": "T", "entity_type": "T", "pk_field": "id"}},
                    "relations": [],
                    "junctions": [],
                },
                user=user,
            )
            await session.commit()

            result = await svc.match(kb.id, tables)
            assert result.status == "exact"
            assert result.template.kb_id is None
            assert result.usable is True
        finally:
            await session.execute(
                ImportMappingTemplate.__table__.delete().where(
                    ImportMappingTemplate.name == "全局模板"
                )
            )
            await session.delete(kb)
            await session.commit()
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_saving_same_name_and_fingerprint_updates_in_place():
    engine, factory = _new_session()
    async with factory() as session:
        user, kb = await _seed_kb(session, f"tpl4-{uuid.uuid4().hex[:8]}")
        tables = {"T": ["id"]}
        fingerprint = dataset_fingerprint(tables)
        svc = MappingTemplateService(session)
        try:
            first = await svc.save(
                kb_id=kb.id, name="同名", fingerprint=fingerprint,
                config={"version": 2, "tables": {}, "relations": [], "junctions": []},
                user=user,
            )
            await session.commit()

            second = await svc.save(
                kb_id=kb.id, name="同名", fingerprint=fingerprint,
                config={
                    "version": 2,
                    "tables": {"T": {"table": "T", "entity_type": "T", "pk_field": "id"}},
                    "relations": [],
                    "junctions": [],
                },
                user=user,
            )
            await session.commit()

            assert first.id == second.id  # 覆盖而非新增
            count = await session.scalar(
                select(ImportMappingTemplate).where(
                    ImportMappingTemplate.name == "同名",
                    ImportMappingTemplate.schema_fingerprint == fingerprint,
                )
            )
            assert count is not None
        finally:
            await session.execute(
                ImportMappingTemplate.__table__.delete().where(
                    ImportMappingTemplate.kb_id == kb.id
                )
            )
            await session.delete(kb)
            await session.commit()
    await engine.dispose()
