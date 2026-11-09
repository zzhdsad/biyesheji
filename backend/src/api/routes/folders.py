"""文件与文件夹管理路由（第一阶段）。

职责：给「普通文档」和「结构化数据」两个空间各维护一棵文件夹树，并提供
创建 / 重命名 / 移动 / 删除 四个动作。文件本身的归属在 documents 路由里
（``/documents/{id}/move``、``/documents/{id}/rename``）。

设计约定（与前端行为一一对应）：
- 文件夹只是组织信息：**移动文件夹或文件都不会触碰物理文件**，
  ``documents.storage_path`` 全程不变，所以不存在重复上传或搬迁丢文件。
- 只允许删除**空**文件夹（无子文件夹且无文件）。这样既满足「删除前检查内容」，
  也不需要引入软删除/回收站，更不可能连带删掉别处的数据。
- 空间之间完全隔离：父目录必须与文件夹同 ``space``，跨空间移动一律 400。
- 权限：本阶段不引入目录级 ACL（用户明确要求不过度设计）。目录是共享的元信息，
  文件可见性仍由 ``documents.kb_id`` 的 KB 权限决定，文件的移动/重命名走 KB 写权限。
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import AppException, NotFoundError
from src.core.file_spaces import FOLDER_NAME_MAX, STRUCTURED_SPACE, normalize_space
from src.domain.models import (
    Document,
    FileFolder,
    StructuredGroup,
    StructuredGroupFile,
    User,
)
from src.infrastructure.database import get_db

router = APIRouter(prefix="/folders", tags=["folders"])

# 祖先链遍历上限：正常层级远小于该值，仅用于防御脏数据造成的死循环
_MAX_ANCESTOR_DEPTH = 100


class FolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    space: str
    name: str
    parent_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    # 直接子级数量（不含递归），供前端判断「能否删除」「是否为空目录」
    document_count: int = 0
    child_count: int = 0


class CreateFolderRequest(BaseModel):
    name: str
    space: str = "document"
    parent_id: uuid.UUID | None = None


class EnsurePathRequest(BaseModel):
    """按相对路径逐级确保目录存在（上传文件夹时保留原始目录结构）。"""

    path: str
    space: str = "document"


class UpdateFolderRequest(BaseModel):
    """重命名 / 移动：两者可单独提交。

    ``parent_id`` 显式传 null 表示「移动到根目录」，字段缺省表示「不改父级」，
    因此用 ``model_fields_set`` 区分，不能靠默认值判断。
    """

    name: str | None = None
    parent_id: uuid.UUID | None = None


def _clean_name(raw: str) -> str:
    """目录名基本校验：非空、无路径分隔符、长度受限。

    目录名只用于展示，不参与物理路径（物理键是 {kb_id}/{doc_id}.{ext}），
    这里仍然过滤分隔符，避免后续任何按名称拼路径的改动把这里变成注入点。
    """
    name = (raw or "").strip()
    if not name:
        raise AppException(400, "文件夹名称不能为空")
    if len(name) > FOLDER_NAME_MAX:
        raise AppException(400, f"文件夹名称最长 {FOLDER_NAME_MAX} 个字符")
    if any(ch in name for ch in ("/", "\\", "\x00")) or name in (".", ".."):
        raise AppException(400, "文件夹名称不能包含路径分隔符")
    return name


async def _get_folder(db: AsyncSession, folder_id: uuid.UUID) -> FileFolder:
    folder = await db.get(FileFolder, folder_id)
    if folder is None:
        raise NotFoundError("文件夹不存在")
    return folder


async def _require_parent(
    db: AsyncSession, space: str, parent_id: uuid.UUID | None
) -> None:
    """父目录必须存在且属于同一空间；None 表示根目录，无需校验。"""
    if parent_id is None:
        return
    parent = await db.get(FileFolder, parent_id)
    if parent is None:
        raise NotFoundError("上级文件夹不存在")
    if parent.space != space:
        raise AppException(400, "上级文件夹与当前文件夹不属于同一空间")


async def _assert_name_free(
    db: AsyncSession,
    space: str,
    parent_id: uuid.UUID | None,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> None:
    stmt = select(FileFolder.id).where(
        FileFolder.space == space,
        FileFolder.name == name,
        FileFolder.parent_id == parent_id if parent_id else FileFolder.parent_id.is_(None),
    )
    if exclude_id is not None:
        stmt = stmt.where(FileFolder.id != exclude_id)
    if await db.scalar(stmt.limit(1)):
        raise AppException(400, f"该目录下已存在同名文件夹「{name}」")


async def _assert_no_cycle(
    db: AsyncSession, folder: FileFolder, new_parent_id: uuid.UUID | None
) -> None:
    """移动文件夹时禁止形成环：新父级不能是自身或自身的后代。"""
    if new_parent_id is None:
        return
    if new_parent_id == folder.id:
        raise AppException(400, "文件夹不能移动到自身之下")
    cursor: uuid.UUID | None = new_parent_id
    for _ in range(_MAX_ANCESTOR_DEPTH):
        if cursor is None:
            return
        if cursor == folder.id:
            raise AppException(400, "文件夹不能移动到自己的子文件夹之下（会形成环）")
        ancestor = await db.get(FileFolder, cursor)
        if ancestor is None:
            raise NotFoundError("上级文件夹不存在")
        if ancestor.space != folder.space:
            raise AppException(400, "上级文件夹与当前文件夹不属于同一空间")
        cursor = ancestor.parent_id
    raise AppException(400, "文件夹层级过深或存在环状数据，已拒绝移动")


async def _count_children(db: AsyncSession, space: str) -> dict[uuid.UUID, int]:
    rows = await db.execute(
        select(FileFolder.parent_id, func.count())
        .where(FileFolder.space == space, FileFolder.parent_id.is_not(None))
        .group_by(FileFolder.parent_id)
    )
    return {parent_id: int(count) for parent_id, count in rows.all()}


async def _count_documents(
    db: AsyncSession, space: str, related: bool | None = None
) -> dict[uuid.UUID, int]:
    filters = [
        Document.space == space,
        Document.deleted_at.is_(None),
        Document.folder_id.is_not(None),
    ]
    if related is not None and space == STRUCTURED_SPACE:
        grouped_doc_ids = (
            select(StructuredGroupFile.doc_id)
            .join(StructuredGroup, StructuredGroup.id == StructuredGroupFile.group_id)
            .where(StructuredGroup.status != "deleted")
        )
        filters.append(
            Document.id.in_(grouped_doc_ids)
            if related
            else Document.id.not_in(grouped_doc_ids)
        )
    rows = await db.execute(
        select(Document.folder_id, func.count())
        .where(*filters)
        .group_by(Document.folder_id)
    )
    return {folder_id: int(count) for folder_id, count in rows.all()}


def _filter_scope_folders(
    folders: list[FileFolder],
    scope_counts: dict[uuid.UUID, int],
    total_counts: dict[uuid.UUID, int],
) -> tuple[list[FileFolder], dict[uuid.UUID, int], set[uuid.UUID]]:
    """按当前页签（关联 / 非关联）过滤目录树。

    结构化空间的两个页签共用同一棵目录树，但文件范围互斥。若一个目录的子树里
    只有**另一个页签**的文件，它就不该出现在当前页签——否则用户看到的是「空目录」，
    删除时却被隐藏文件挡住（两个页签互相干扰）。

    规则：
    - 目录自身或任一后代有当前页签范围内的存活文件 → 显示（保证导航链路完整）
    - 子树里完全没有存活文件（真正可删除的空目录）→ 两个页签都显示
    """
    by_id = {f.id: f for f in folders}

    def with_ancestors(ids: set[uuid.UUID]) -> set[uuid.UUID]:
        out = set(ids)
        for fid in ids:
            cursor = by_id.get(fid)
            while cursor is not None and cursor.parent_id is not None:
                parent = by_id.get(cursor.parent_id)
                if parent is None or parent.id in out:
                    break
                out.add(parent.id)
                cursor = parent
        return out

    visible = with_ancestors(set(scope_counts.keys()))
    # 子树里没有任何存活文件的目录（含空目录、只含回收站文件的目录）
    occupied = with_ancestors(set(total_counts.keys()))
    visible |= {f.id for f in folders if f.id not in occupied}

    kept = [f for f in folders if f.id in visible]
    scope_counts = {k: v for k, v in scope_counts.items() if k in visible}
    return kept, scope_counts, visible


def _to_out(
    folder: FileFolder,
    doc_counts: dict[uuid.UUID, int],
    child_counts: dict[uuid.UUID, int],
) -> FolderOut:
    return FolderOut(
        id=folder.id,
        space=folder.space,
        name=folder.name,
        parent_id=folder.parent_id,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
        document_count=doc_counts.get(folder.id, 0),
        child_count=child_counts.get(folder.id, 0),
    )


@router.get("", response_model=list[FolderOut])
async def list_folders(
    space: str = Query("document"),
    related: bool | None = Query(
        None,
        description="仅结构化空间生效：目录里的文件计数按关联/非关联范围统计",
    ),
    db: AsyncSession = Depends(get_db),
) -> list[FolderOut]:
    """返回该空间全部文件夹（扁平列表，前端按 parent_id 组树并拼面包屑）。

    一次性给出直接子级计数，前端无需逐目录再请求，就能标出空目录与非空目录。
    """
    space = normalize_space(space)
    folders = list(
        await db.scalars(
            select(FileFolder)
            .where(FileFolder.space == space)
            .order_by(FileFolder.parent_id.asc().nulls_first(), FileFolder.name)
        )
    )
    doc_counts = await _count_documents(db, space, related)
    child_counts = await _count_children(db, space)
    if related is not None and space == STRUCTURED_SPACE:
        # 两个页签共用目录树但文件互斥：只属于另一个页签的目录不在这里展示，
        # 避免「页签里看着是空目录、删除却被隐藏文件挡住」。
        folders, doc_counts, visible = _filter_scope_folders(
            folders, doc_counts, await _count_documents(db, space, None)
        )
        child_counts = {k: v for k, v in child_counts.items() if k in visible}
    return [_to_out(f, doc_counts, child_counts) for f in folders]


@router.post("", response_model=FolderOut, status_code=201)
async def create_folder(
    body: CreateFolderRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> FolderOut:
    """新建文件夹：名称由用户自定义，可指定上级；同空间同层级重名直接 400。"""
    user: User = request.state.user
    space = normalize_space(body.space)
    name = _clean_name(body.name)
    parent_id = body.parent_id
    await _require_parent(db, space, parent_id)
    await _assert_name_free(db, space, parent_id, name)

    folder = FileFolder(space=space, name=name, parent_id=parent_id)
    db.add(folder)
    try:
        await db.commit()
    except IntegrityError as exc:  # 并发下同名：由数据库唯一索引兜底
        await db.rollback()
        logger.warning(f"并发创建同名文件夹 space={space} parent={parent_id} name={name!r}: {exc}")
        raise AppException(400, f"该目录下已存在同名文件夹「{name}」") from exc
    await db.refresh(folder)

    logger.info(f"新建文件夹 id={folder.id} space={space} parent={parent_id} name={name!r}")
    return _to_out(folder, {}, {})


@router.post("/ensure", response_model=FolderOut, status_code=201)
async def ensure_folder_path(
    body: EnsurePathRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> FolderOut:
    """按 ``a/b/c`` 相对路径逐级确保目录存在，返回最深一级文件夹。

    第三阶段「上传文件夹批量导入」用它保留原始目录结构：前端把浏览器给出的
    ``webkitRelativePath`` 去掉文件名后传进来，同一批文件里相同的目录只会命中
    同一棵树，已存在的层级直接复用、缺失的层级补建，因此重复上传同一个目录不会
    产生「副本(1)」这类并列目录，也不会覆盖或搬动任何物理文件。

    层级名沿用 ``_clean_name`` 的校验（非空、无路径分隔符、长度受限），
    空路径（根目录）返回根哨兵以外的处理：直接 400，让调用方按「上传到根目录」
    的语义改用 folder_id=None。
    """
    space = normalize_space(body.space)
    segments = [part for part in (body.path or "").replace("\\", "/").split("/") if part.strip()]
    if not segments:
        raise AppException(400, "目录路径为空，请直接以根目录上传")
    names = [_clean_name(part) for part in segments]
    if len(names) > _MAX_ANCESTOR_DEPTH:
        raise AppException(400, f"目录层级最多 {_MAX_ANCESTOR_DEPTH} 级")

    parent_id: uuid.UUID | None = None
    current: FileFolder | None = None
    for name in names:
        found = await db.scalar(
            select(FileFolder)
            .where(
                FileFolder.space == space,
                FileFolder.name == name,
                FileFolder.parent_id == parent_id if parent_id else FileFolder.parent_id.is_(None),
            )
            .limit(1)
        )
        if found is None:
            new_folder = FileFolder(space=space, name=name, parent_id=parent_id)
            try:
                # 逐层级建：并发撞同名目录时只回滚这一层（SAVEPOINT），
                # 已经建好的上层目录不会跟着丢。
                async with db.begin_nested():
                    db.add(new_folder)
            except IntegrityError as exc:
                logger.warning(f"并发创建同名文件夹 space={space} parent={parent_id} name={name!r}: {exc}")
            found = await db.scalar(
                select(FileFolder)
                .where(
                    FileFolder.space == space,
                    FileFolder.name == name,
                    FileFolder.parent_id == parent_id
                    if parent_id
                    else FileFolder.parent_id.is_(None),
                )
                .limit(1)
            )
            if found is None:
                raise AppException(400, f"该目录下已存在同名文件夹「{name}」") from exc
        await db.refresh(found)
        parent_id = found.id
        current = found

    if current is None:  # names 已保证非空，留明确的错误而不是 assert
        raise AppException(400, "目录路径无效")
    await db.commit()
    logger.info(f"确保目录链 space={space} path={'/'.join(names)!r} -> id={current.id}")
    return _to_out(current, {}, {})


@router.patch("/{folder_id}", response_model=FolderOut)
async def update_folder(
    folder_id: uuid.UUID,
    body: UpdateFolderRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> FolderOut:
    """重命名和/或移动整个文件夹（子树随父级一起移动，只改 parent_id/name）。"""
    folder = await _get_folder(db, folder_id)
    want_name = body.name is not None
    # 显式传 parent_id（含 null）才移动；缺省表示不动父级
    want_parent = "parent_id" in body.model_fields_set
    if not want_name and not want_parent:
        raise AppException(400, "没有需要更新的字段")

    new_name = _clean_name(body.name) if want_name else folder.name
    new_parent_id = body.parent_id if want_parent else folder.parent_id

    if want_parent:
        await _assert_no_cycle(db, folder, new_parent_id)
        await _require_parent(db, folder.space, new_parent_id)
        # 子文件夹会跟随移动：若新父级下已有同名子目录，移动后必然撞唯一索引，
        # 这里提前拦住，避免只挪一半留下冲突。
        clash = await db.scalar(
            select(FileFolder.id)
            .where(
                FileFolder.space == folder.space,
                FileFolder.parent_id == new_parent_id,
                FileFolder.name == new_name,
                FileFolder.id != folder.id,
            )
            .limit(1)
        )
        if clash:
            raise AppException(400, f"目标目录下已存在同名文件夹「{new_name}」")
    elif new_name != folder.name:
        await _assert_name_free(
            db, folder.space, folder.parent_id, new_name, exclude_id=folder.id
        )

    folder.name = new_name
    if want_parent:
        folder.parent_id = new_parent_id
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise AppException(400, f"文件夹名称冲突：「{new_name}」已存在") from exc
    await db.refresh(folder)

    doc_counts = await _count_documents(db, folder.space)
    child_counts = await _count_children(db, folder.space)
    return _to_out(folder, doc_counts, child_counts)


@router.delete("/{folder_id}")
async def delete_folder(
    folder_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除文件夹：只允许删除空目录。

    「非空即拒绝」而不是级联删除，是为了满足「删除前检查是否包含文件或子文件夹」
    和「避免误删其他目录中的数据」：任何情况下本接口都不会连带删掉文件或其他目录。
    """
    folder = await _get_folder(db, folder_id)

    child_count = int(
        await db.scalar(
            select(func.count()).select_from(FileFolder).where(FileFolder.parent_id == folder.id)
        )
        or 0
    )
    doc_count = int(
        await db.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.folder_id == folder.id, Document.deleted_at.is_(None))
        )
        or 0
    )
    if child_count or doc_count:
        parts: list[str] = []
        if child_count:
            parts.append(f"{child_count} 个子文件夹")
        if doc_count:
            parts.append(f"{doc_count} 个文件")
        raise AppException(
            400,
            f"文件夹「{folder.name}」非空（含 {'、'.join(parts)}），"
            "请先移动其中的内容再删除",
        )

    name = folder.name
    space = folder.space
    # 只剩回收站里的文件时（列表上看是空目录），先解除它们对目录的引用再删目录。
    # 否则这些软删除文件的 folder_id 外键会阻止删除，前端只会看到一条数据库报错。
    detached = int(
        await db.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.folder_id == folder.id)
        )
        or 0
    )
    if detached:
        await db.execute(
            update(Document).where(Document.folder_id == folder.id).values(folder_id=None)
        )
        logger.info(
            f"文件夹内仅剩回收站文件，解除目录引用 id={folder_id} count={detached}"
        )
    await db.delete(folder)
    await db.commit()
    logger.info(f"删除空文件夹 id={folder_id} space={space} name={name!r}")
    return {"id": str(folder_id), "message": f"文件夹「{name}」已删除"}


@router.get("/path/{folder_id}", response_model=list[FolderOut])
async def get_folder_path(
    folder_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[FolderOut]:
    """从根目录到该文件夹的祖先链（前端直接渲染面包屑，不必自己回溯）。"""
    folder = await _get_folder(db, folder_id)
    chain = [folder]
    cursor = folder.parent_id
    for _ in range(_MAX_ANCESTOR_DEPTH):
        if cursor is None:
            break
        ancestor = await db.get(FileFolder, cursor)
        if ancestor is None:  # 数据异常（悬空 parent_id）：截断而非报错，面包屑仍可展示
            logger.error(f"文件夹祖先链断裂 id={folder_id} missing_parent={cursor}")
            break
        chain.append(ancestor)
        cursor = ancestor.parent_id
    chain.reverse()
    return [
        FolderOut(
            id=f.id,
            space=f.space,
            name=f.name,
            parent_id=f.parent_id,
            created_at=f.created_at,
            updated_at=f.updated_at,
        )
        for f in chain
    ]
