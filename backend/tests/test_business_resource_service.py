"""业务资源写入服务测试（隔离测试库）。

覆盖：
- D1/D2/D6/canon 表写 theories/prescriptions/herbs/literatures；
- 重复写入按 name 幂等，不新增行；
- D7 属性表补全中药性味/归经（含二次导入不重复补全）；
- D4 组成表写 prescription_ingredients（比例 + 单位）；
- 实体显示名识别中文语义列（Chinese_term 等）。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.application.business_resource_service import BusinessResourceService
from src.application.entity_import_service import _display_name
from src.application.tabular_parser import ParsedRow
from src.core.config import settings
from src.domain.models import (
    Herb,
    Literature,
    Prescription,
    PrescriptionIngredient,
    Theory,
)


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


def _rows(records: list[dict]) -> list[ParsedRow]:
    return [ParsedRow(row_no=i, data=r) for i, r in enumerate(records, start=2)]


async def _cleanup(session, tag: str) -> None:
    """只删除本测试创建的资源（名称带随机 tag），不动任何已有数据。"""
    prescription_ids = select(Prescription.id).where(Prescription.name.like(f"%{tag}%"))
    await session.execute(
        delete(PrescriptionIngredient).where(
            PrescriptionIngredient.prescription_id.in_(prescription_ids)
        )
    )
    await session.execute(delete(Prescription).where(Prescription.name.like(f"%{tag}%")))
    # 先删组成（方剂删除已级联），再删中药，避免 herb_id RESTRICT 阻止删除
    await session.execute(delete(Herb).where(Herb.name.like(f"%{tag}%")))
    await session.execute(delete(Theory).where(Theory.name.like(f"%{tag}%")))
    await session.execute(delete(Literature).where(Literature.name.like(f"%{tag}%")))
    await session.commit()


def test_display_name_recognizes_chinese_semantic_columns():
    assert _display_name({"CHP_ID": "C1", "Chinese_herbal_pieces": "阿氏蒿"}, "C1") == "阿氏蒿"
    assert _display_name({"TCMT_ID": "T1", "Chinese_term": "急则治标"}, "T1") == "急则治标"
    assert (
        _display_name({"CPM_ID": "M1", "Chinese_patent_medicine": "安乐胶囊"}, "M1")
        == "安乐胶囊"
    )
    assert _display_name({"Herb_ID": "H1", "Chinese_Character": "人参"}, "H1") == "人参"
    # 没有任何名称列时仍退回主键值（不编造）
    assert _display_name({"CHP_ID": "C1"}, "C1") == "C1"


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_resource_tables_are_written_and_idempotent():
    engine, factory = _new_session()
    async with factory() as session:
        tag = uuid.uuid4().hex[:8]
        tables = {
            "D1_TCM_terminology": _rows(
                [
                    {
                        "TCMT_ID": f"T{tag}",
                        "Chinese_term": f"术语{tag}",
                        "Chinese_synonyms": "别名甲、别名乙",
                        "English_definition_description": "A treatment principle.",
                        "Chinese_group": "治则",
                    }
                ]
            ),
            "D2_Chinese_patent_medicine": _rows(
                [
                    {
                        "CPM_ID": f"M{tag}",
                        "Chinese_patent_medicine": f"方剂{tag}",
                        "Routes_of_administration": "Oral",
                    }
                ]
            ),
            "D6_Chinese_herbal_pieces": _rows(
                [
                    {
                        "CHP_ID": f"C{tag}",
                        "Chinese_herbal_pieces": f"中药{tag}",
                        "Chinese_synonyms": "别名丙",
                        "Sources": "Viridiplantae",
                    }
                ]
            ),
            "canon": _rows(
                [
                    {
                        "id": f"canon-{tag}",
                        "title": f"文献{tag}",
                        "author": "作者甲",
                        "dynasty": "清",
                        "text": "正文内容",
                        "work_family": "丛书甲",
                    }
                ]
            ),
        }
        svc = BusinessResourceService(session)
        try:
            stats = await svc.write_tables(
                rows_by_table=tables, files_by_table={}, batch_id=f"test-{tag}"
            )
            await session.commit()

            assert not stats.errors
            assert (
                stats.theories,
                stats.prescriptions,
                stats.herbs,
                stats.literatures,
            ) == (1, 1, 1, 1)

            theory = await session.scalar(
                select(Theory).where(Theory.name == f"术语{tag}")
            )
            assert theory is not None
            assert theory.aliases == ["别名甲", "别名乙"]
            assert theory.content == "A treatment principle."
            assert theory.source == f"D1_TCM_terminology:T{tag}"

            prescription = await session.scalar(
                select(Prescription).where(Prescription.name == f"方剂{tag}")
            )
            assert prescription is not None
            assert prescription.usage_method == "Oral"
            assert prescription.source == f"D2_Chinese_patent_medicine:M{tag}"

            herb = await session.scalar(select(Herb).where(Herb.name == f"中药{tag}"))
            assert herb is not None
            assert herb.aliases == ["别名丙"]
            assert herb.source == f"D6_Chinese_herbal_pieces:C{tag}"

            literature = await session.scalar(
                select(Literature).where(Literature.name == f"文献{tag}")
            )
            assert literature is not None
            assert (literature.author, literature.dynasty) == ("作者甲", "清")
            assert literature.content == "正文内容"
            assert literature.aliases == ["丛书甲"]

            # 重复导入：不新增任何业务行，全部计为已存在
            again = await svc.write_tables(
                rows_by_table=tables, files_by_table={}, batch_id=f"test2-{tag}"
            )
            await session.commit()
            assert (
                again.theories,
                again.prescriptions,
                again.herbs,
                again.literatures,
            ) == (0, 0, 0, 0)
            assert again.existing == 4
        finally:
            await _cleanup(session, tag)
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
async def test_properties_and_composition_enrich_resources():
    engine, factory = _new_session()
    async with factory() as session:
        tag = uuid.uuid4().hex[:8]
        chp = f"C{tag}"
        cpm = f"M{tag}"
        tables = {
            "D6_Chinese_herbal_pieces": _rows(
                [{"CHP_ID": chp, "Chinese_herbal_pieces": f"中药{tag}"}]
            ),
            "D2_Chinese_patent_medicine": _rows(
                [{"CPM_ID": cpm, "Chinese_patent_medicine": f"方剂{tag}"}]
            ),
            "D7_CHP_Medicinal_properties": _rows(
                [
                    {
                        "CHP_ID": chp,
                        "Class": "Medicinal flavor",
                        "Medicinal_properties": "Pungent medicinal",
                    },
                    {
                        "CHP_ID": chp,
                        "Class": "Therapeutic nature",
                        "Medicinal_properties": "Warm therapeutic",
                    },
                    {
                        "CHP_ID": chp,
                        "Class": "Meridian tropism",
                        "Medicinal_properties": "Liver meridian",
                    },
                ]
            ),
            "D4_CPM_CHP": _rows([{"CPM_ID": cpm, "CHP_ID": chp, "Dosage_ratio": "0.125"}]),
        }
        svc = BusinessResourceService(session)
        try:
            stats = await svc.write_tables(
                rows_by_table=tables, files_by_table={}, batch_id=f"test-{tag}"
            )
            await session.commit()

            assert not stats.errors
            assert (stats.herbs, stats.prescriptions) == (1, 1)
            assert stats.enriched == 1
            assert stats.ingredients == 1

            herb = await session.scalar(select(Herb).where(Herb.name == f"中药{tag}"))
            assert herb is not None
            assert herb.properties == "性味：Pungent medicinal；四气：Warm therapeutic"
            assert herb.channels == ["Liver meridian"]

            ingredient = await session.scalar(
                select(PrescriptionIngredient).where(
                    PrescriptionIngredient.herb_id == herb.id
                )
            )
            assert ingredient is not None
            assert float(ingredient.amount) == 0.13  # Numeric(10,2) 四舍五入
            assert ingredient.unit == "比例"

            # 二次导入：属性已填充不再重复补全，组成不重复
            again = await svc.write_tables(
                rows_by_table=tables, files_by_table={}, batch_id=f"test2-{tag}"
            )
            await session.commit()
            assert (again.enriched, again.ingredients) == (0, 0)
        finally:
            await _cleanup(session, tag)
    await engine.dispose()
