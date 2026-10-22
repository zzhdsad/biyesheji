"""真实中医知识数据导入任务（导入中心）落地逻辑。

设计原则（严格遵守用户约束）：
- **不重新实现**数据库写入与向量化：documents 走现有
  `DocumentService.upload` → `ParseService.run` → `IndexingService.run`
  （即 Document → Chunk → Embedding(BGE-M3) → Milvus 的既有链路）；
  resources（herb/prescription/theory/literature）直接落各自的业务表，
  保持与资源 CRUD 路由一致的字段语义。
- 任务状态机 pending → processing → completed / failed（可 cancelled），
  模式复用 revectorize_service（后台线程 + 独立 engine + 协作式取消）。
- 数据清洗只做"去空值 / 去完全重复 / 保留溯源"，**不伪造**缺失字段。
- 批次标识：每次导入生成 batch_id，写入各业务行的 import_batch_id，
  便于后续统计与按批次回滚。
"""

from __future__ import annotations

import asyncio
import io
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import UploadFile
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.application import dataset_scanner
from src.application.document_service import DocumentService
from src.application.indexing_service import IndexingService
from src.application.parse_service import ParseService
from src.core.config import settings
from src.core.exceptions import AppException
from src.core.source_meta import ERAS, SOURCE_TYPE_CLASSIC
from src.domain.models import (
    Herb,
    ImportJob,
    KnowledgeBaseResource,
    Literature,
    Prescription,
    Theory,
)
from src.application.resource_vector_service import ResourceVectorService

# 同 revectorize：每处理 N 条提交一次
_COMMIT_EVERY = 5
# 孤儿任务判定（心跳超时的进行中任务视为中断）
_ORPHAN_AFTER_SECONDS = 30 * 60
# 文件名净化（去掉路径分隔符与控制字符）
_UNSAFE_FILENAME = re.compile(r"[\\/:*?\"<>|\r\n\t]")

# 字段候选键（按数据语义排序，取第一个非空值；支持点为分隔的嵌套路径）
_NAME_KEYS = [
    "title",
    "Chinese_herbal_pieces",
    "Chinese_patent_medicine",
    "Chinese_term",
    "name",
    "file_name",
    "Chinese_Character",  # AromaTCM herb_basic：中药中文名
]
_ALIAS_KEYS = ["Chinese_synonyms", "work_family", "aliases"]
_AUTHOR_KEYS = ["author"]
_DYNASTY_KEYS = ["dynasty"]
_CONTENT_KEYS = ["content.0.text", "text", "content"]
_SOURCE_ID_KEYS = ["id", "CHP_ID", "CPM_ID", "TCMT_ID", "Herb_ID"]

# 中药业务字段候选键（AromaTCM herb_basic 等带功效/性味/归经的数据集）：
# 缺失即留空，绝不为了凑字段而生成别名或翻译原文。
_HERB_QI_KEYS = ["Properties_and_actions_of_TCM"]  # 四气
_HERB_FLAVOR_KEYS = ["Flavors"]  # 五味
_HERB_CHANNEL_KEYS = ["Meridians"]  # 归经
_HERB_EFFECT_KEYS = ["Efficacy"]  # 功效
_HERB_TOXICITY_KEYS = ["Toxicity"]  # 毒性
_HERB_LATIN_KEYS = ["Latin_Name"]  # 拉丁药材名
_HERB_PINYIN_KEYS = ["Pinyin_Name"]  # 拼音
_HERB_PLANT_KEYS = ["Source_Plant_Latin_Names"]  # 原植物（基原）

_EMPTY_TOKENS = {"", "NA", "N/A", "null", "None", "不详", "未知", "-"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── 取值与清洗 ──────────────────────────────────────────────────────────────


def _pick_first(record: dict[str, Any], keys: list[str]) -> str:
    """按候选键顺序取第一个非空值；支持 `content.0.text` 形式的嵌套路径。"""
    for key in keys:
        if "." in key:
            cur: Any = record
            ok = True
            for part in key.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                    cur = cur[int(part)]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, (str, int, float)):
                value = str(cur).strip()
                if value not in _EMPTY_TOKENS:
                    return value
            continue
        value = record.get(key)
        if isinstance(value, (str, int, float)):
            value = str(value).strip()
            if value not in _EMPTY_TOKENS:
                return value
    return ""


def _split_aliases(raw: str) -> list[str]:
    """别名拆分（、，, ; ；/ ），去重去空，长度受字段约束。"""
    if not raw:
        return []
    parts = re.split(r"[、,，;；/|]+", raw)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and p not in _EMPTY_TOKENS and p not in out:
            out.append(p[:128])
    return out


def _collapse_ws(raw: str) -> str:
    """压缩单元格内的换行/连续空白（数据文件常含裸 CR），去首尾空白。"""
    return re.sub(r"\s+", " ", raw or "").strip()


def _split_list(raw: str) -> list[str]:
    """按顿号/逗号/分号拆分多值字段（如 Meridians=`liver、kidney`）。"""
    if not raw:
        return []
    out: list[str] = []
    for part in re.split(r"[、,，;；/|]+", raw):
        part = _collapse_ws(part)
        if part and part not in _EMPTY_TOKENS and part not in out:
            out.append(part[:32])
    return out


def _normalize_era(dynasty: str) -> str | None:
    """朝代 → 受控年代枚举；无法可靠映射时返回 None（不猜测）。"""
    if dynasty in ERAS:
        return dynasty
    for era in ERAS:
        if era and era in dynasty:
            return era
    return None


def _safe_filename(title: str, fallback: str) -> str:
    base = _UNSAFE_FILENAME.sub("_", (title or "").strip())[:120] or fallback
    return f"{base}.txt"


def _dedupe_key(target_type: str, values: dict[str, Any]) -> str:
    """批次内去重键：资源类按名称，文档类按标题+内容长度。"""
    name = values.get("name") or values.get("title") or ""
    if target_type == "document":
        content = values.get("content") or ""
        return f"{name}::{len(content)}::{hash(content[:512])}"
    return name


# ── 记录 → 系统字段（按检测出的目标类型）──────────────────────────────────


def _build_values(
    target_type: str,
    record: dict[str, Any],
    *,
    dataset_name: str,
    source_file: str,
) -> dict[str, Any]:
    """把一条原始记录整理成系统字段（缺失即留空，不伪造）。"""
    name = _pick_first(record, _NAME_KEYS)
    # 书名形如 `000-神农本草经.txt` → 去掉序号与扩展名
    if name.lower().endswith((".txt", ".md")):
        name = name.rsplit(".", 1)[0]
    name = re.sub(r"^\d+[-.、]\s*", "", name).strip()

    source_id = _pick_first(record, _SOURCE_ID_KEYS)
    source = f"{dataset_name}:{source_id}" if source_id else f"{dataset_name}:{source_file}"

    values: dict[str, Any] = {
        "name": name,
        "aliases": _split_aliases(_pick_first(record, _ALIAS_KEYS)),
        "author": _pick_first(record, _AUTHOR_KEYS),
        "dynasty": _pick_first(record, _DYNASTY_KEYS),
        "content": _pick_first(record, _CONTENT_KEYS),
        "source": source[:255],
        "source_file": source_file,
    }
    # 中成药的给药途径 → usage_method（原文保留，不翻译）
    values["usage_method"] = _pick_first(record, ["Routes_of_administration"])[:255]

    # 中药业务内容（AromaTCM herb_basic 等数据集才有；其他数据集取值为空，
    # 落库时保持为空，不生成占位文本）
    qi = _collapse_ws(_pick_first(record, _HERB_QI_KEYS))
    flavor = _collapse_ws(_pick_first(record, _HERB_FLAVOR_KEYS))
    props_parts = []
    if qi:
        props_parts.append(f"四气：{qi}")
    if flavor:
        props_parts.append(f"五味：{flavor}")
    values["properties"] = "；".join(props_parts)[:255]
    values["channels"] = _split_list(_pick_first(record, _HERB_CHANNEL_KEYS))
    values["effects"] = _collapse_ws(_pick_first(record, _HERB_EFFECT_KEYS))

    desc_parts: list[str] = []
    toxicity = _collapse_ws(_pick_first(record, _HERB_TOXICITY_KEYS))
    if toxicity:
        desc_parts.append(f"毒性：{toxicity}")
    pinyin = _collapse_ws(_pick_first(record, _HERB_PINYIN_KEYS))
    if pinyin:
        desc_parts.append(f"拼音：{pinyin}")
    latin = _collapse_ws(_pick_first(record, _HERB_LATIN_KEYS))
    if latin:
        desc_parts.append(f"拉丁药材名：{latin}")
    plant = _collapse_ws(_pick_first(record, _HERB_PLANT_KEYS))
    if plant:
        desc_parts.append(f"原植物：{plant}")
    values["description"] = "；".join(desc_parts)
    return values


async def _name_taken(session: AsyncSession, target_type: str, name: str) -> bool:
    model = {"herb": Herb, "prescription": Prescription, "theory": Theory, "literature": Literature}.get(target_type)
    if model is None or not name:
        return False
    row = await session.scalar(select(model.id).where(model.name == name))
    return row is not None


async def _enrich_existing_herb(
    session: AsyncSession,
    name: str,
    values: dict[str, Any],
) -> tuple[bool, list[str], list[dict]]:
    """同名中药已存在时**只补全空业务字段**（性味/归经/功效/描述），不覆盖已有值。

    场景：AromaTCM「中医基本表」里有 113 味与 TCM-MKG 已有中药同名，而后者只有
    名称+出处（无功效/性味/归经）。逐条覆盖会丢失既有溯源信息，一律跳过又会让
    RAG 继续无内容可答 —— 因此只填充当前为空的字段。

    Returns:
        (是否发生变更, 补全的字段名列表, 重新向量化失败清单)
    """
    from src.application.resource_vector_service import ResourceVectorService

    herb = (await session.scalars(select(Herb).where(Herb.name == name))).first()
    if herb is None:
        return False, [], []

    filled: list[str] = []
    if values.get("properties") and not (herb.properties or "").strip():
        herb.properties = values["properties"]
        filled.append("properties")
    if values.get("channels") and not (herb.channels or []):
        herb.channels = values["channels"]
        filled.append("channels")
    if values.get("effects") and not (herb.effects or "").strip():
        herb.effects = values["effects"]
        filled.append("effects")
    if values.get("description") and not (herb.description or "").strip():
        herb.description = values["description"]
        filled.append("description")
    if not filled:
        return False, [], []

    await session.flush()
    # PG 内容已变 → 同步刷新所有已挂载 KB 的向量，避免"内容更新了但向量过期"
    failures: list[dict] = []
    try:
        svc = ResourceVectorService()
        await svc.revectorize_all_mounts(session, herb, "herb")
        failures = list(svc.last_revectorize_failures or [])
    except Exception as exc:  # noqa: BLE001 - 向量化失败不回滚 PG 补全，但要留下线索
        logger.error(f"中药内容补全后重新向量化失败 name={name}: {exc}")
        failures = [{"kb_id": "*", "error": str(exc)}]
    return True, filled, failures


async def _insert_resource(
    session: AsyncSession,
    target_type: str,
    values: dict[str, Any],
    *,
    dataset_name: str,
    batch_id: str,
    kb_id: uuid.UUID | None = None,
) -> None:
    """写入资源表；指定 kb_id 时**挂载并向量化**，使其真正进入 RAG 检索链路。

    资源（herb/prescription/theory/literature）只有挂载到知识库并写入向量后
    才能被检索命中；否则 PG 里有数据、向量库里没有，必然"证据不足"拒答。
    挂载与向量化复用现有 KnowledgeBaseResource + ResourceVectorService，
    不另写一套写入逻辑。
    """
    common = {
        "source_dataset": dataset_name[:128],
        "import_batch_id": batch_id,
    }
    if target_type == "herb":
        obj: Any = Herb(
            name=values["name"],
            aliases=values["aliases"],
            source=values["source"],
            # 业务内容（缺失即空，不伪造）
            properties=values.get("properties") or "",
            channels=values.get("channels") or [],
            effects=values.get("effects") or "",
            description=values.get("description") or "",
            **common,
        )
    elif target_type == "prescription":
        obj = Prescription(
            name=values["name"],
            aliases=values["aliases"],
            usage_method=values["usage_method"],
            source=values["source"],
            **common,
        )
    elif target_type == "theory":
        obj = Theory(
            name=values["name"],
            aliases=values["aliases"],
            content=values["content"],
            source=values["source"],
            **common,
        )
    elif target_type == "literature":
        obj = Literature(
            name=values["name"],
            aliases=values["aliases"],
            author=values["author"],
            dynasty=values["dynasty"],
            content=values["content"],
            source=values["source"],
            **common,
        )
    else:  # pragma: no cover - 调用方已校验类型
        raise ValueError(f"不支持的资源类型：{target_type}")

    session.add(obj)
    await session.flush()  # 拿到 id，向量化失败可整体回滚

    if kb_id is None:
        return

    exists = await session.scalar(
        select(KnowledgeBaseResource).where(
            KnowledgeBaseResource.knowledge_base_id == kb_id,
            KnowledgeBaseResource.resource_type == target_type,
            KnowledgeBaseResource.resource_id == obj.id,
        )
    )
    if exists is None:
        session.add(
            KnowledgeBaseResource(
                knowledge_base_id=kb_id,
                resource_type=target_type,
                resource_id=obj.id,
            )
        )
        await session.flush()
    svc = ResourceVectorService()
    await asyncio.to_thread(svc.vectorize_and_store, obj, kb_id=kb_id, resource_type=target_type)


async def _insert_document(
    session: AsyncSession,
    values: dict[str, Any],
    *,
    kb_id: uuid.UUID,
    dataset_name: str,
    batch_id: str,
    vectorize: bool,
) -> uuid.UUID:
    """走既有文档链路：DocumentService.upload → ParseService → IndexingService。

    Returns:
        新建 documents.id
    """
    raw = (values["content"] or "").encode("utf-8")
    # 古籍类数据固定标注为「经典古籍」；年代由朝代枚举推导，推不出则留空
    era = _normalize_era(values["dynasty"])
    upload = UploadFile(
        file=io.BytesIO(raw),
        filename=_safe_filename(values["name"], f"import-{batch_id}"),
        size=len(raw),
    )
    doc = await DocumentService(session).upload(
        kb_id,
        upload,
        None,  # 导入为后台管理动作，不走 KB 成员鉴权（路由层已限制 admin）
        source_type=SOURCE_TYPE_CLASSIC,
        era=era,
    )
    # 批次溯源（upload 内部已 commit，这里补写后再提交）
    doc.source_dataset = dataset_name[:128]
    doc.import_batch_id = batch_id
    await session.commit()

    await ParseService(session).run(doc.id)
    if vectorize:
        await IndexingService(session).run(doc.id)
    return doc.id


# ── 任务状态机 ──────────────────────────────────────────────────────────────

_CANCEL_FLAGS: dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()
# 任务级"是否向量化"开关（进程内，随任务结束清理；不为此改表结构）
_VECTORIZE_FLAGS: dict[str, bool] = {}


def request_cancel(job_id: uuid.UUID) -> None:
    with _CANCEL_LOCK:
        _CANCEL_FLAGS.setdefault(str(job_id), threading.Event()).set()


def is_cancel_requested(job_id: uuid.UUID) -> bool:
    with _CANCEL_LOCK:
        ev = _CANCEL_FLAGS.get(str(job_id))
    return bool(ev and ev.is_set())


def _clear_cancel_flag(job_id: uuid.UUID) -> None:
    with _CANCEL_LOCK:
        _CANCEL_FLAGS.pop(str(job_id), None)


async def reap_orphan_jobs(db: AsyncSession) -> int:
    """回收心跳超时的进行中任务（服务重启场景）。"""
    cutoff = _now() - timedelta(seconds=_ORPHAN_AFTER_SECONDS)
    rows = (
        await db.scalars(
            select(ImportJob).where(
                ImportJob.status.in_(["pending", "processing"]),
                ImportJob.updated_at < cutoff,
            )
        )
    ).all()
    for job in rows:
        job.status = "failed"
        job.error_message = "任务中断（服务重启或进程退出）"
        job.finished_at = _now()
    if rows:
        await db.commit()
        logger.warning(f"回收 {len(rows)} 个中断的导入任务")
    return len(rows)


async def _active_job(db: AsyncSession) -> ImportJob | None:
    return (
        await db.scalars(
            select(ImportJob)
            .where(ImportJob.status.in_(["pending", "processing"]))
            .order_by(ImportJob.created_at.desc())
        )
    ).first()


async def create_job(
    db: AsyncSession,
    *,
    dataset_id: str,
    target_type: str,
    kb_id: uuid.UUID | None = None,
    limit: int | None = None,
    user_id: uuid.UUID | None = None,
    vectorize: bool = True,
    confirmed: bool = False,
) -> ImportJob:
    """创建导入任务（pending）。**必须显式确认**才可创建（禁止扫描后自动导入）。"""
    if not confirmed:
        raise AppException(400, "请先确认字段映射与导入范围后再创建任务")
    if target_type not in ("herb", "prescription", "theory", "literature", "document"):
        raise AppException(400, f"不支持的导入类型：{target_type}")
    if target_type == "document" and kb_id is None:
        raise AppException(400, "导入为文档时必须指定目标知识库")
    # 资源类：指定 kb_id 时会一并挂载 + 向量化（否则数据进不了 RAG 检索链路）

    await reap_orphan_jobs(db)
    if await _active_job(db) is not None:
        raise AppException(409, "已有导入任务在执行中，请等待其完成后再发起")

    profile = next(
        (d for d in dataset_scanner.scan_source_dir().datasets if d.dataset_id == dataset_id),
        None,
    )
    if profile is None:
        raise AppException(404, f"数据集不存在：{dataset_id}")
    if profile.detected_type == "unknown":
        raise AppException(400, "该数据集类型未识别，需人工确认映射后再导入")
    if profile.detected_type == "relation":
        raise AppException(400, "该数据集为关联/属性表，暂不支持直接导入（需先导入主表）")

    # 安全阀：单次导入条数受 IMPORT_MAX_RECORDS_PER_JOB 限制（禁止一次全量导入百万级语料）
    cap = min(limit or settings.IMPORT_MAX_RECORDS_PER_JOB, settings.IMPORT_MAX_RECORDS_PER_JOB)
    job = ImportJob(
        batch_id=uuid.uuid4().hex[:16],
        dataset_id=dataset_id,
        dataset_name=profile.name,
        source_file=profile.relative_path,
        target_type=target_type,
        kb_id=kb_id,
        status="pending",
        total=cap,
        created_by=user_id,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    _VECTORIZE_FLAGS[str(job.id)] = vectorize
    logger.info(
        f"创建导入任务 job_id={job.id} batch={job.batch_id} dataset={dataset_id} "
        f"type={target_type} limit={cap} vectorize={vectorize}"
    )
    return job


def start_job(job_id: uuid.UUID) -> None:
    """后台线程执行导入（HTTP 立即返回）。"""
    Thread = threading.Thread
    Thread(target=_run_in_thread, args=(job_id,), name=f"import-{job_id}", daemon=True).start()


def _run_in_thread(job_id: uuid.UUID) -> None:
    try:
        asyncio.run(_execute(job_id))
    except Exception:  # noqa: BLE001
        logger.exception(f"导入任务异常终止 job_id={job_id}")
    finally:
        _VECTORIZE_FLAGS.pop(str(job_id), None)


async def _execute(job_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    vectorize = _VECTORIZE_FLAGS.get(str(job_id), True)
    try:
        async with Session() as session:
            job = await session.get(ImportJob, job_id)
            if job is None:
                logger.error(f"导入任务不存在 job_id={job_id}")
                return
            if is_cancel_requested(job_id):
                job.status = "cancelled"
                job.started_at = _now()
                job.finished_at = _now()
                await session.commit()
                return

            job.status = "processing"
            job.started_at = _now()
            job.error_message = ""
            await session.commit()

            try:
                profile, records = await asyncio.to_thread(
                    dataset_scanner.read_records, job.dataset_id, job.total or None
                )
            except Exception as exc:  # noqa: BLE001
                job.status = "failed"
                job.error_message = f"读取数据集失败：{exc}"
                job.finished_at = _now()
                await session.commit()
                logger.error(f"导入任务读取数据集失败 job_id={job_id}: {exc}")
                return

            job.total = len(records)
            job.dataset_name = profile.name
            await session.commit()

            failures: list[dict[str, Any]] = []
            seen: set[str] = set()
            succeeded = 0
            skipped = 0
            enriched_count = 0
            processed = 0
            cancelled = False

            for index, record in enumerate(records, start=1):
                if is_cancel_requested(job_id):
                    cancelled = True
                    break
                source_file = str(record.get("_source_file") or profile.relative_path)
                values = _build_values(
                    job.target_type,
                    record,
                    dataset_name=profile.name,
                    source_file=source_file,
                )
                try:
                    # 1) 空值清理：正文为空的文档无检索价值，直接跳过
                    if job.target_type == "document" and not values["content"].strip():
                        skipped += 1
                        continue
                    # 2) 名称缺失：资源类不自动生成标题（禁止伪造）；
                    #    文档类用"数据集名+序号"作为**技术性标题**（非书目字段）
                    if not values["name"]:
                        if job.target_type != "document":
                            skipped += 1
                            failures.append(
                                {
                                    "index": index,
                                    "name": "",
                                    "reason": "缺少名称字段，已跳过（不自动生成标题）",
                                }
                            )
                            continue
                        values["name"] = f"{profile.name}-{index}"
                    # 2) 去重：批次内 + 库内同名
                    key = _dedupe_key(job.target_type, values)
                    if key in seen:
                        skipped += 1
                        continue
                    seen.add(key)
                    if job.target_type != "document" and await _name_taken(session, job.target_type, values["name"]):
                        # 同名资源：带业务内容的数据集（如 AromaTCM）只补全空字段，
                        # 空包名插入会撞唯一约束；二者都不覆盖已有内容。
                        enriched, fields_filled, revec_failures = False, [], []
                        if job.target_type == "herb":
                            enriched, fields_filled, revec_failures = await _enrich_existing_herb(
                                session, values["name"], values
                            )
                        if enriched:
                            succeeded += 1
                            enriched_count += 1
                            if revec_failures:
                                failures.append(
                                    {
                                        "index": index,
                                        "name": values["name"],
                                        "reason": f"已补字段 {','.join(fields_filled)}，"
                                        f"但重新向量化失败：{revec_failures}",
                                    }
                                )
                            continue
                        skipped += 1
                        continue

                    if job.target_type == "document":
                        await _insert_document(
                            session,
                            values,
                            kb_id=job.kb_id,  # type: ignore[arg-type]
                            dataset_name=profile.name,
                            batch_id=job.batch_id,
                            vectorize=vectorize,
                        )
                    else:
                        await _insert_resource(
                            session,
                            job.target_type,
                            values,
                            dataset_name=profile.name,
                            batch_id=job.batch_id,
                            kb_id=job.kb_id,
                        )
                    succeeded += 1
                except IntegrityError:
                    await session.rollback()
                    skipped += 1
                except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
                    await session.rollback()
                    failures.append(
                        {
                            "index": index,
                            "name": values["name"],
                            "reason": str(exc)[:300],
                        }
                    )
                    logger.warning(f"导入失败 job_id={job_id} index={index}: {exc}")
                finally:
                    # 进度与统计的统一出口：无论走插入 / 补全空字段 / 跳过 / 失败，
                    # 每条记录都必须推进 processed，保证
                    # processed == succeeded + failed + skipped（此前各 continue
                    # 分支绕过了赋值，导致任务显示 processed 小于实际处理量）。
                    processed = index
                    job.processed = index
                    job.succeeded = succeeded
                    job.skipped = skipped
                    job.failed = len(failures)
                    if index % _COMMIT_EVERY == 0 or index == len(records):
                        job.failed_items = failures[-200:]
                        await session.commit()

            if cancelled:
                job.status = "cancelled"
                job.error_message = f"已取消：已处理 {processed}/{len(records)}"
            else:
                # 有成功记录即视为完成（失败明细可查）；全部失败才算 failed
                job.status = "completed" if succeeded > 0 else "failed"
            job.processed = processed
            job.succeeded = succeeded
            job.skipped = skipped
            job.failed = len(failures)
            job.failed_items = failures[-200:]
            job.error_message = (
                job.error_message or ("；".join(f["reason"] for f in failures[-5:])[:2000] if failures else "")
            )
            job.finished_at = _now()
            await session.commit()
            logger.info(
                f"导入任务结束 job_id={job.id} status={job.status} total={job.total} "
                f"succeeded={succeeded} skipped={skipped} failed={len(failures)} "
                f"enriched={enriched_count}"
            )
    finally:
        _clear_cancel_flag(job_id)
        await engine.dispose()


# ── 查询 ────────────────────────────────────────────────────────────────────


def job_to_dict(job: ImportJob) -> dict[str, Any]:
    total = job.total or 0
    percent = round(job.processed / total * 100, 1) if total else 0.0
    return {
        "job_id": str(job.id),
        "batch_id": job.batch_id,
        "dataset_id": job.dataset_id,
        "dataset_name": job.dataset_name,
        "source_file": job.source_file,
        "target_type": job.target_type,
        "kb_id": str(job.kb_id) if job.kb_id else None,
        "status": job.status,
        "total": total,
        "processed": job.processed,
        "succeeded": job.succeeded,
        "failed": job.failed,
        "skipped": job.skipped,
        "percent": percent,
        "failed_items": job.failed_items or [],
        "error_message": job.error_message,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


async def get_job(db: AsyncSession, job_id: uuid.UUID) -> ImportJob:
    job = await db.get(ImportJob, job_id)
    if job is None:
        raise AppException(404, "导入任务不存在")
    return job


async def latest_job(db: AsyncSession) -> ImportJob | None:
    return (await db.scalars(select(ImportJob).order_by(ImportJob.created_at.desc()))).first()


async def list_jobs(db: AsyncSession, limit: int = 20) -> list[ImportJob]:
    rows = await db.scalars(select(ImportJob).order_by(ImportJob.created_at.desc()).limit(limit))
    return list(rows)


async def cancel_job(db: AsyncSession, job_id: uuid.UUID) -> ImportJob:
    job = await db.get(ImportJob, job_id)
    if job is None:
        raise AppException(404, "导入任务不存在")
    if job.status not in ("pending", "processing"):
        raise AppException(400, f"任务已结束（{job.status}），无需取消")
    job.status = "cancelled"
    job.error_message = "取消请求已提交，任务将在当前记录处理完成后停止"
    await db.commit()
    request_cancel(job_id)
    return job
