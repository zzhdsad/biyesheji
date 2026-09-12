"""用户管理路由（仅 admin 可访问）。

功能：
- GET    /users                    ：用户列表（不含已软删除）
- POST   /users                    ：单个添加用户
- PUT    /users/{id}               ：更新用户信息
- DELETE /users/{id}               ：软删除用户（进回收站，7天内可恢复）
- POST   /users/{id}/reset-password：重置用户密码
- POST   /users/batch-delete       ：批量软删除（全量预检，有任何错误则不执行）
- GET    /users/trash              ：回收站列表（自动清理超过7天的）
- POST   /users/{id}/restore       ：从回收站恢复用户
- DELETE /users/{id}/purge         ：彻底删除（不可恢复）
- GET    /users/template           ：下载批量导入 CSV 模板
- POST   /users/batch              ：批量导入 CSV

权限：所有接口仅 admin 角色可调用（RBAC）。
"""

import csv
import io
import re
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import PermissionDeniedError
from src.core.security import generate_random_password, hash_password
from src.domain.models import User
from src.infrastructure.database import get_db

router = APIRouter(prefix="/users", tags=["users"])

ALLOWED_ROLES = {"admin", "member", "viewer"}
CSV_HEADERS = ["username", "name", "department", "email", "role"]
TRASH_RETENTION_DAYS = 7  # 回收站保留天数


# ── Schemas ──────────────────────────────────────────────────────────────────


class UserOut(BaseModel):
    """用户列表/详情返回。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    username: str
    role: str
    name: str = ""
    department: str = ""
    must_change_password: bool = False
    is_active: bool = True
    created_at: datetime | None = None
    deleted_at: datetime | None = None


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    email: EmailStr
    name: str = Field(default="", max_length=64)
    department: str = Field(default="", max_length=64)
    role: str = Field(default="member")

    @field_validator("username")
    @classmethod
    def _username_alnum(cls, v: str) -> str:
        if not re.match(r"^[A-Za-z0-9_\-]+$", v):
            raise ValueError("用户名仅支持字母、数字、下划线、短横线")
        return v

    @field_validator("role")
    @classmethod
    def _role_valid(cls, v: str) -> str:
        if v not in ALLOWED_ROLES:
            raise ValueError(f"角色必须是 {ALLOWED_ROLES} 之一")
        return v


class UserCreateResponse(BaseModel):
    user: UserOut
    initial_password: str


class UserUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    department: str | None = Field(default=None, max_length=64)
    role: str | None = None

    @field_validator("role")
    @classmethod
    def _role_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in ALLOWED_ROLES:
            raise ValueError(f"角色必须是 {ALLOWED_ROLES} 之一")
        return v


class BatchImportResult(BaseModel):
    total: int
    success: int
    failed: int
    errors: list[str]


class ResetPasswordResponse(BaseModel):
    user_id: uuid.UUID
    username: str
    new_password: str


class BatchDeleteRequest(BaseModel):
    user_ids: list[uuid.UUID] = Field(min_length=1)


class BatchDeleteResult(BaseModel):
    """批量删除结果（软删除）。"""

    total: int
    success: int
    message: str = "已移入回收站，7 天内可恢复"


class BatchDeleteErrorResponse(BaseModel):
    """批量删除预检失败返回（HTTP 400）。"""

    message: str = "以下用户无法删除，未执行任何操作"
    errors: list[str]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _require_admin(request: Request) -> User:
    user: User = request.state.user
    if user.role != "admin":
        raise PermissionDeniedError("仅管理员可操作用户")
    return user


async def _get_active_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """查活跃用户（未软删除）。"""
    return await db.scalar(
        select(User).where(User.id == user_id, User.deleted_at.is_(None))
    )


async def _purge_expired_trash(db: AsyncSession) -> None:
    """清理回收站中超过 7 天的用户（硬删除）。"""
    cutoff = datetime.utcnow() - timedelta(days=TRASH_RETENTION_DAYS)
    expired = (
        await db.scalars(
            select(User).where(User.deleted_at < cutoff)
        )
    ).all()
    for u in expired:
        await db.delete(u)
    if expired:
        await db.commit()


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("", response_model=list[UserOut])
async def list_users(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[User]:
    """活跃用户列表（不含已软删除）。"""
    _require_admin(request)
    rows = (
        await db.scalars(
            select(User)
            .where(User.deleted_at.is_(None))
            .order_by(User.created_at.desc())
        )
    ).all()
    return list(rows)


@router.post("", response_model=UserCreateResponse, status_code=201)
async def create_user(
    request: Request,
    payload: UserCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> UserCreateResponse:
    """单个添加用户（仅 admin）。"""
    _require_admin(request)

    # 唯一性校验：不过滤 deleted_at，防止恢复后冲突
    if await db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status_code=400, detail="邮箱已被使用")
    if await db.scalar(select(User).where(User.username == payload.username)):
        raise HTTPException(status_code=400, detail="用户名已被使用")

    initial_password = generate_random_password(8)
    user = User(
        username=payload.username,
        email=payload.email,
        name=payload.name,
        department=payload.department,
        role=payload.role,
        hashed_password=hash_password(initial_password),
        must_change_password=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserCreateResponse(user=UserOut.model_validate(user), initial_password=initial_password)


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    request: Request,
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> User:
    """更新用户信息（仅 admin）。"""
    _require_admin(request)
    user = await _get_active_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if payload.name is not None:
        user.name = payload.name
    if payload.department is not None:
        user.department = payload.department
    if payload.role is not None:
        user.role = payload.role
    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/{user_id}")
async def delete_user(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """软删除用户（移入回收站，7 天内可恢复）。"""
    current = _require_admin(request)
    if current.id == user_id:
        raise HTTPException(status_code=400, detail="不能删除自己")

    user = await _get_active_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在或已删除")

    user.deleted_at = datetime.utcnow()
    await db.commit()
    return {"message": "用户已移入回收站，7 天内可恢复"}


@router.post("/{user_id}/disable")
async def disable_user(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """禁用用户账号（BUSINESS_RULES §2 离职处理=禁用，不删除，保留数据，可恢复）。

    禁用后用户无法登录，但所有数据（文档、问答记录）保留。
    与回收站删除不同：禁用是永久状态直到手动启用，不受 7 天自动清理影响。
    """
    current = _require_admin(request)
    if current.id == user_id:
        raise HTTPException(status_code=400, detail="不能禁用自己")

    user = await _get_active_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在或已删除")

    user.is_active = False
    await db.commit()
    return {"message": "用户已禁用（离职处理），账号数据保留，可随时启用恢复"}


@router.post("/{user_id}/enable")
async def enable_user(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """启用用户账号（恢复已禁用的用户）。"""
    _require_admin(request)

    user = await _get_active_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在或已删除")

    user.is_active = True
    await db.commit()
    return {"message": "用户已启用，可正常登录"}


@router.post("/{user_id}/reset-password", response_model=ResetPasswordResponse)
async def reset_user_password(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> ResetPasswordResponse:
    """管理员重置用户密码（仅 admin）。"""
    current = _require_admin(request)
    if current.id == user_id:
        raise HTTPException(status_code=400, detail="不能重置自己的密码，请使用修改密码功能")

    user = await _get_active_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    new_password = generate_random_password(8)
    user.hashed_password = hash_password(new_password)
    user.must_change_password = True
    await db.commit()
    await db.refresh(user)
    return ResetPasswordResponse(
        user_id=user.id,
        username=user.username,
        new_password=new_password,
    )


@router.post("/batch-delete")
async def batch_delete_users(
    request: Request,
    payload: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """批量软删除（仅 admin）。

    先做全量预检，若有任何一个用户无法删除（不存在/已在回收站/是自己），
    直接返回 HTTP 400 + 错误详情列表，**不执行任何删除操作**。
    全部通过后统一设置 deleted_at。
    """
    current = _require_admin(request)

    # ── 预检 ──
    errors: list[str] = []
    for uid in payload.user_ids:
        if current.id == uid:
            errors.append(f"用户 {uid}：不能删除自己")
            continue
        user = await db.scalar(select(User).where(User.id == uid))
        if user is None:
            errors.append(f"用户 {uid}：不存在")
        elif user.deleted_at is not None:
            errors.append(f"用户 {uid}：已在回收站")

    if errors:
        raise HTTPException(
            status_code=400,
            detail=BatchDeleteErrorResponse(
                errors=errors,
            ).model_dump(),
        )

    # ── 全部通过 → 统一软删除 ──
    now = datetime.utcnow()
    await db.execute(
        update(User)
        .where(User.id.in_(payload.user_ids))
        .values(deleted_at=now)
    )
    await db.commit()
    return BatchDeleteResult(total=len(payload.user_ids), success=len(payload.user_ids))


@router.post("/batch-restore")
async def batch_restore_users(
    request: Request,
    payload: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """批量从回收站恢复（仅 admin）。

    全量预检：用户必须在回收站中、邮箱/用户名在活跃用户中无冲突。
    任何一项失败返回 HTTP 400 + 错误详情，不执行任何恢复。
    """
    _require_admin(request)

    errors: list[str] = []
    # 先把要恢复的用户全取出来
    to_restore: list[User] = []
    users_by_id = {u.id: u for u in (await db.scalars(select(User))).all()}

    for uid in payload.user_ids:
        u = users_by_id.get(uid)
        if u is None:
            errors.append(f"用户 {uid}：不存在")
            continue
        if u.deleted_at is None:
            errors.append(f"用户 {u.username}：不在回收站中")
            continue
        to_restore.append(u)

    # 冲突检查：恢复后 email/username 不能与活跃用户冲突
    active_emails = {u.email for u in users_by_id.values() if u.deleted_at is None}
    active_usernames = {u.username for u in users_by_id.values() if u.deleted_at is None}
    for u in to_restore:
        if u.email in active_emails:
            errors.append(f"用户 {u.username}：邮箱 {u.email} 已被占用，无法恢复")
        if u.username in active_usernames:
            errors.append(f"用户 {u.username}：用户名已被占用，无法恢复")

    if errors:
        raise HTTPException(
            status_code=400,
            detail={"message": "以下用户无法恢复，未执行任何操作", "errors": errors},
        )

    for u in to_restore:
        u.deleted_at = None
    await db.commit()
    return {
        "total": len(payload.user_ids),
        "success": len(to_restore),
        "message": f"已恢复 {len(to_restore)} 个用户",
    }


# ── 回收站 ──────────────────────────────────────────────────────────────────


@router.get("/trash", response_model=list[UserOut])
async def list_trash(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[User]:
    """回收站列表（仅 admin）。

    进入时自动清理超过 7 天的已删除用户（彻底移除，不可恢复）。
    """
    _require_admin(request)
    await _purge_expired_trash(db)
    rows = (
        await db.scalars(
            select(User)
            .where(User.deleted_at.isnot(None))
            .order_by(User.deleted_at.desc())
        )
    ).all()
    return list(rows)


@router.post("/{user_id}/restore", response_model=UserOut)
async def restore_user(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> User:
    """从回收站恢复用户（仅 admin）。"""
    _require_admin(request)
    user = await db.scalar(
        select(User).where(User.id == user_id, User.deleted_at.isnot(None))
    )
    if user is None:
        raise HTTPException(status_code=404, detail="回收站中不存在该用户")

    # 恢复前检查唯一性（如果邮箱/用户名在删除期间被占用了）
    conflict = await db.scalar(
        select(User).where(
            (User.email == user.email) | (User.username == user.username),
            User.deleted_at.is_(None),
            User.id != user.id,
        )
    )
    if conflict is not None:
        raise HTTPException(
            status_code=400,
            detail=f"无法恢复：{'邮箱' if conflict.email == user.email else '用户名'}已被其他用户占用",
        )

    user.deleted_at = None
    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/{user_id}/purge")
async def purge_user(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """彻底删除用户（不可恢复，仅 admin）。"""
    _require_admin(request)
    user = await db.scalar(
        select(User).where(User.id == user_id, User.deleted_at.isnot(None))
    )
    if user is None:
        raise HTTPException(status_code=404, detail="回收站中不存在该用户")

    username = user.username
    await db.delete(user)
    await db.commit()
    return {"message": f"用户 {username} 已彻底删除，不可恢复"}


# ── 导入 / 模板（不变） ────────────────────────────────────────────────────


@router.get("/template")
async def download_template(request: Request) -> StreamingResponse:
    """下载批量导入 CSV 模板（仅 admin）。"""
    _require_admin(request)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADERS)
    writer.writerow(["zhangsan", "张三", "技术部", "zhangsan@company.com", "member"])
    writer.writerow(["lisi", "李四", "产品部", "lisi@company.com", "viewer"])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=user_import_template.csv"},
    )


@router.post("/batch", response_model=BatchImportResult)
async def batch_import_users(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> BatchImportResult:
    """批量导入用户（仅 admin）。

    CSV 表头：username,name,department,email,role
    - role 可选 admin/member/viewer，缺省为 member
    - 邮箱/用户名重复（包括软删除的）跳过并记录错误
    - 系统为每个新用户生成随机初始密码（不在响应中返回，管理员需另行通知）
    """
    _require_admin(request)

    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None or not set(CSV_HEADERS).issubset(set(reader.fieldnames)):
        raise HTTPException(
            status_code=400,
            detail=f"CSV 表头必须包含：{', '.join(CSV_HEADERS)}",
        )

    errors: list[str] = []
    success = 0
    total = 0

    for idx, row in enumerate(reader, start=2):
        total += 1
        username = (row.get("username") or "").strip()
        email = (row.get("email") or "").strip()
        name = (row.get("name") or "").strip()
        department = (row.get("department") or "").strip()
        role = (row.get("role") or "member").strip()

        if not username or not email:
            errors.append(f"第 {idx} 行：用户名和邮箱不能为空")
            continue
        if not re.match(r"^[A-Za-z0-9_\-]+$", username):
            errors.append(f"第 {idx} 行：用户名 {username} 格式不合法")
            continue
        if role not in ALLOWED_ROLES:
            errors.append(f"第 {idx} 行：角色 {role} 不合法（仅 admin/member/viewer）")
            continue

        # 唯一性校验：不过滤软删除（防止恢复冲突）
        if await db.scalar(select(User).where(User.email == email)):
            errors.append(f"第 {idx} 行：邮箱 {email} 已存在")
            continue
        if await db.scalar(select(User).where(User.username == username)):
            errors.append(f"第 {idx} 行：用户名 {username} 已存在")
            continue

        initial_password = generate_random_password(8)
        user = User(
            username=username,
            email=email,
            name=name,
            department=department,
            role=role,
            hashed_password=hash_password(initial_password),
            must_change_password=True,
        )
        db.add(user)
        success += 1

    await db.commit()
    return BatchImportResult(total=total, success=success, failed=len(errors), errors=errors)
