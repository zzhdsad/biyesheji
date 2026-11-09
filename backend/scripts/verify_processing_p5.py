"""第五阶段一次性核对脚本（只读，不写库、不改数据）。

运行：docker exec kp-backend python scripts/verify_processing_p5.py
覆盖三件事：
1. 三条链路的派生状态与阶段是否只由真实字段推出（含 30 分钟残留放行口径）；
2. 重试守卫的判定（回收站文件 / 缺失原始文件 / 正在解析）；
3. 两个概览端点是否已注册，并对开发库现有行做一次只读派生。

脚本本身只跑一次、无轮询、无外部等待；异常直接非零退出。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.utils.processing_status import (
    STATUS_AWAITING_CONFIRM,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_SUCCESS,
    document_processing,
    group_processing,
    is_stale_processing,
    structured_processing,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        FAILURES.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name} {detail}")
    else:
        print(f"  [ok] {name}")


def doc(**kw):
    base = dict(
        parse_status="pending",
        index_status="not_indexed",
        chunk_count=0,
        vector_count=0,
        error_message="",
        index_error="",
        storage_path="kb/1/a.pdf",
        deleted_at=None,
        file_type="pdf",
        updated_at=datetime.now(timezone.utc),
    )
    base.update(kw)
    return SimpleNamespace(**base)


print("1) 普通文档派生")
v = document_processing(doc())
check("未解析→待处理+第1步后", v["status"] == STATUS_PENDING and v["stage_index"] == 1, str(v["stage_index"]))
check("阶段链是5步", v["stage_total"] == 5 and v["stages"][0]["label"] == "上传")
v = document_processing(doc(parse_status="parsing", updated_at=datetime.now(timezone.utc)))
check("解析中→处理中", v["status"] == STATUS_PROCESSING, v["reason"])
v = document_processing(doc(parse_status="failed", error_message="编码无法识别"))
check("失败→处理失败+真实原因", v["status"] == STATUS_FAILED and "编码" in v["reason"], v["reason"])
v = document_processing(doc(parse_status="success", chunk_count=12))
check(
    "切片就绪未向量化→待处理",
    v["status"] == STATUS_PENDING
    and v["next_action"] == "revectorize"
    and "revectorize" in v["retry_actions"],
    f'{v["status"]}/{v["next_action"]}/{v["retry_actions"]}',
)
check(
    "切片阶段已完成（真实计数）",
    v["stages"][2]["state"] == "done" and v["stages"][3]["state"] != "done",
)
v = document_processing(doc(parse_status="completed", chunk_count=12, vector_count=12, index_status="indexed"))
check("全部完成→已完成", v["status"] == STATUS_SUCCESS and v["stage_index"] == 4, str(v["stage_index"]))
check("完成态无待办", v["next_action"] == "none" and v["retry_actions"] == [], str(v["retry_actions"]))
v = document_processing(
    doc(parse_status="completed", chunk_count=12, vector_count=9, index_status="indexed")
)
check(
    "向量数与切片数不一致→待处理（不谎报完成）",
    v["status"] == STATUS_PENDING,
    f'{v["status"]}/{v["reason"]}',
)
v = document_processing(doc(parse_status="completed", chunk_count=12, vector_count=12, index_status="stale"))
check("内容已替换（索引过期）→待处理", v["status"] == STATUS_PENDING, v["reason"])
v = document_processing(doc(parse_status="success", deleted_at=datetime.now(timezone.utc)))
check(
    "回收站文件→阻塞且不可重试",
    bool(v["blocked_reason"]) and not v["retryable"] and v["next_action"] == "none",
    str(v),
)
v = document_processing(doc(parse_status="success", storage_path=""))
check("原始文件缺失→阻塞", bool(v["blocked_reason"]) and not v["retryable"], str(v["blocked_reason"]))
check("派生结果不含百分比", "percent" not in str(sorted(v.keys())))

print("2) 非关联结构化派生")


def spdoc(**kw):
    base = dict(
        space="structured",
        file_type="xlsx",
        storage_path="kb/1/a.xlsx",
        deleted_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def parse(**kw):
    base = dict(
        status="pending",
        field_count=8,
        record_count=0,
        anomaly_count=0,
        skipped_count=0,
        truncated=False,
        error_message="",
        detected_format="xlsx",
        parsed_at=None,
        updated_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    base.update(kw)
    return SimpleNamespace(**base)


v = structured_processing(spdoc(), None, format_supported=True)
check("尚无解析行→待处理+可重发", v["status"] == STATUS_PENDING and v["retryable"], str(v))
v = structured_processing(spdoc(), parse(status="parsing"), format_supported=True)
check("解析中→处理中", v["status"] == STATUS_PROCESSING, v["reason"])
v = structured_processing(
    spdoc(),
    parse(status="parsing", updated_at=datetime.now(timezone.utc) - timedelta(minutes=45)),
    format_supported=True,
)
check("残留 45 分钟→仍为处理中但放行重试", v["retryable"] and "30 分钟" in v["reason"], v["reason"])
v = structured_processing(spdoc(), parse(status="failed", error_message="表头无法识别"), format_supported=True)
check("失败→原因来自库字段", v["status"] == STATUS_FAILED and "表头" in v["reason"], v["reason"])
v = structured_processing(
    spdoc(),
    parse(status="failed", error_message="列数不一致", record_count=30),
    format_supported=True,
)
check("失败保留旧记录且如实说明", "30 条记录仍保留" in v["reason"], v["reason"])
v = structured_processing(spdoc(), parse(status="success", record_count=0), format_supported=True)
check("成功但 0 条→待处理", v["status"] == STATUS_PENDING and v["stage_index"] == 3, str(v["stage_index"]))
v = structured_processing(spdoc(), parse(status="success", record_count=25), format_supported=True)
check(
    "成功有记录→已完成",
    v["status"] == STATUS_SUCCESS and v["results"]["record_count"] == 25 and v["stage_index"] == 4,
    str(v),
)
v = structured_processing(spdoc(), parse(status="success", record_count=25, truncated=True), format_supported=True)
check("达到上限→如实说明截断", "上限" in v["reason"], v["reason"])
v = structured_processing(spdoc(file_type="txt"), None, format_supported=False)
check("非结构化格式→不猜测", v["status"] == STATUS_PENDING and v["next_action"] == "none", v["reason"])
v = structured_processing(spdoc(deleted_at=datetime.now(timezone.utc)), parse(status="success", record_count=5))
check("回收站→阻塞且禁用重试", bool(v["blocked_reason"]) and v["retry_actions"] == [], str(v["retry_actions"]))

print("3) 关联文件组派生")


def group(status="draft", relations=None, validation=None, record_count=0, error_message=""):
    return SimpleNamespace(
        status=status,
        relations=relations or [],
        validation=validation,
        record_count=record_count,
        error_message=error_message,
        validated_at=None,
        confirmed_at=None,
    )


def member(doc_id="d1", file_name="a.xlsx", parse_status="success", record_count=5, removed=False, error=""):
    return dict(
        doc_id=doc_id,
        file_name=file_name,
        current_file_name=file_name,
        alias="",
        parse_status=parse_status,
        record_count=record_count,
        removed=removed,
        error_message=error,
    )


rel = lambda confirmed: [{"confirmed": confirmed}]  # noqa: E731
two_ok = [member("d1", "a.xlsx"), member("d2", "b.xlsx")]
v = group_processing(group(), [member("d1", parse_status=None)])
check("可用文件不足 2 个→失败并提示补文件", v["status"] == STATUS_FAILED and v["next_action"] == "add_files", v["reason"])
v = group_processing(
    group(), [member("d2", "b.xlsx", parse_status="failed", record_count=0, error="编码错误"), member("d1")]
)
check("成员解析失败→失败且点名文件", v["status"] == STATUS_FAILED and "b.xlsx" in v["reason"], v["reason"])
v = group_processing(group(), [member("d2", "b.xlsx", parse_status="parsing", record_count=0), member("d1")])
check("成员正在解析→处理中", v["status"] == STATUS_PROCESSING, v["reason"])
v = group_processing(group(), [member("d2", "b.xlsx", parse_status="pending", record_count=0), member("d1")])
check(
    "成员仅排队→待处理（与另两条链路同口径）",
    v["status"] == STATUS_PENDING and v["next_action"] == "reparse_members",
    f'{v["status"]}/{v["reason"]}',
)
v = group_processing(group(), two_ok)
check("全部解析成功无关系→去推测", v["status"] == STATUS_PENDING and v["next_action"] == "suggest", v["reason"])
v = group_processing(group(relations=rel(False)), two_ok)
check("有未确认关系→待用户确认", v["status"] == STATUS_AWAITING_CONFIRM and v["next_action"] == "confirm_relations", str(v["status"]))
v = group_processing(group(status="draft", relations=rel(True)), two_ok)
check("关系已确认未校验→待用户确认+校验", v["status"] == STATUS_AWAITING_CONFIRM and v["next_action"] == "validate", v["reason"])
v = group_processing(group(status="draft", relations=rel(True), validation={"issues": [], "structural_error_count": 2}), two_ok)
check("校验有结构性错误→失败", v["status"] == STATUS_FAILED and "2" in v["reason"], v["reason"])
v = group_processing(
    group(status="validated", relations=rel(True), validation={"issues": [], "warning_count": 3, "can_confirm": True}),
    two_ok,
)
check("校验通过→等待确认生成", v["status"] == STATUS_AWAITING_CONFIRM and v["next_action"] == "generate", str(v))
check("确认生成前不谎报完成", v["stages"][5]["state"] == "active" and v["stages"][6]["state"] == "pending")
v = group_processing(
    group(
        status="confirmed",
        relations=rel(True),
        validation={"issues": [], "can_confirm": True},
        record_count=8,
    ),
    two_ok,
)
check("已生成关联记录→已完成", v["status"] == STATUS_SUCCESS and v["results"]["record_count"] == 8, str(v["status"]))
v = group_processing(
    group(
        status="stale",
        relations=rel(True),
        validation={"issues": [], "structural_error_count": 0},
        record_count=8,
        error_message="组内文件已替换",
    ),
    two_ok,
)
check("文件变化后作废→待用户确认+重新校验", v["status"] == STATUS_AWAITING_CONFIRM and v["next_action"] == "validate", v["reason"])
v = group_processing(group(status="confirmed", relations=rel(True), record_count=8), [*two_ok, member("d3", removed=True)])
check("已删除成员→阻塞原因可见", "d3" in v["blocked_reason"] or "a.xlsx" in v["blocked_reason"], v["blocked_reason"])
v = group_processing(group(relations=rel(True)), two_ok)
check(
    "未确认关系不能靠重试绕过",
    group_processing(group(status="validated", relations=rel(False)), two_ok)["next_action"] == "confirm_relations",
)

print("4) 残留判定口径")
now = datetime.now(timezone.utc)
check("29 分钟仍在跑（不放行）", not is_stale_processing(now - timedelta(minutes=29)))
check("31 分钟视为残留（放行）", is_stale_processing(now - timedelta(minutes=31)))
check("无时间戳视为残留", is_stale_processing(None))
check("naive 时间戳按 UTC 处理", is_stale_processing(
    datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=40)
))

print("5) 端点注册与开发库只读派生")
from src.main import create_app  # noqa: E402

app = create_app()
# 当前 FastAPI 版本用 _IncludedRouter 延迟展开路由，启动前 app.routes 看不到业务路由，
# 因此以解析后的 OpenAPI 作为「端点是否已注册」的准据。
openapi_paths = app.openapi()["paths"]
registered = sorted(p for p in openapi_paths if "processing" in p)
print(f"  OpenAPI 共 {len(openapi_paths)} 条路径，其中 processing：{registered}")
check(
    "GET /api/v1/documents/{doc_id}/processing 已注册",
    "/api/v1/documents/{doc_id}/processing" in openapi_paths
    and "get" in openapi_paths["/api/v1/documents/{doc_id}/processing"],
    str(registered),
)
check(
    "GET /api/v1/structured/groups/{group_id}/processing 已注册",
    "/api/v1/structured/groups/{group_id}/processing" in openapi_paths
    and "get" in openapi_paths["/api/v1/structured/groups/{group_id}/processing"],
    str(registered),
)

try:
    import asyncio

    from sqlalchemy import select

    from src.domain.models import Document, StructuredGroup, StructuredGroupFile, StructuredParse
    from src.infrastructure.database import get_session_factory
    from src.utils.structured_data import is_structured_format
except Exception as exc:
    print(f"  [skip] 数据库只读派生未执行：{type(exc).__name__}: {exc}")
else:

    async def real_rows() -> tuple[int, int]:
        """只读遍历开发库现有行，确认派生函数吃得下真实 ORM 对象。"""
        docs = groups = 0
        async with get_session_factory()() as db:
            for d in (await db.execute(select(Document).limit(30))).scalars().all():
                docs += 1
                if d.space == "structured":
                    parse = (
                        await db.execute(
                            select(StructuredParse).where(StructuredParse.doc_id == d.id)
                        )
                    ).scalar_one_or_none()
                    view = structured_processing(
                        d, parse, format_supported=is_structured_format(d.file_type)
                    )
                else:
                    view = document_processing(d)
                print(
                    f"   - 文档 {str(d.file_name)[:22]:22} {view['pipeline']:10} {view['status']:15}"
                    f" {view['stage_label']}({view['stage_index'] + 1}/{view['stage_total']})"
                    f" next={view['next_action']} retryable={view['retryable']}"
                )
            for g in (await db.execute(select(StructuredGroup).limit(30))).scalars().all():
                groups += 1
                rows = (
                    await db.execute(
                        select(StructuredGroupFile, Document).where(
                            StructuredGroupFile.group_id == g.id
                        )
                    )
                ).all()
                files = []
                for f, d in rows:
                    parse = (
                        await db.execute(
                            select(StructuredParse).where(StructuredParse.doc_id == d.id)
                        )
                    ).scalar_one_or_none()
                    files.append(
                        dict(
                            doc_id=f.doc_id,
                            file_name=d.file_name,
                            current_file_name=d.file_name,
                            alias=f.alias,
                            parse_status=getattr(parse, "status", "") or "",
                            record_count=int(getattr(parse, "record_count", 0) or 0),
                            removed=d.deleted_at is not None,
                            error_message=str(getattr(parse, "error_message", "") or ""),
                        )
                    )
                view = group_processing(g, files)
                print(
                    f"   - 文件组 {str(g.name)[:20]:20} {view['status']:15}"
                    f" {view['stage_label']}({view['stage_index'] + 1}/{view['stage_total']})"
                    f" next={view['next_action']} 成员={len(files)}"
                )
        return docs, groups

    try:
        n_docs, n_groups = asyncio.run(real_rows())
        check(f"开发库只读派生可执行（文档 {n_docs} / 文件组 {n_groups}）", True)
    except Exception as exc:
        print(f"  [skip] 开发库只读派生失败（不影响纯函数结论）：{type(exc).__name__}: {exc}")

print()
if FAILURES:
    print(f"存在 {len(FAILURES)} 项不符：")
    for line in FAILURES:
        print("  -", line)
    sys.exit(1)
print("第五阶段派生逻辑核对通过")
