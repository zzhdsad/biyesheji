"""第六轮 Batch 6（质量与仓库卫生收尾）回归测试。

本批的定位是**不改动业务规则**的质量收尾，因此大量用例属于"锁定用例"：
把当前（刻意保留的）行为固化成断言，将来一旦有人顺手改动，测试会立刻报警。

覆盖：
- BUG-063 evidence_id 不得混入展示位次 source_index（必须是稳定主键）
- BUG-064 KG 证据编号与分组顺序的已知差异（本批不改排序规则，仅锁定）
- BUG-065 非流式 Citation 保留 KG 关系字段（kg_relation / kg_hop / kg_provenance）
- BUG-066 SSE 协议稳定：不新增事件类型，done.answer 为最终权威答案
- BUG-068 _require_admin 与 client_ip 单一实现
- BUG-069 回收站保留天数口径统一
- BUG-070 统计口径排除已删除数据
- BUG-071 batch_restore 预检不再全表加载
- BUG-072 审计 IP 取值经可信代理门禁

需要 PostgreSQL 的用例统一 skip（其余为纯逻辑用例）。
"""

import pytest

from src.application.evidence import hit_to_evidence

from tests.test_chat import _upload_doc
from tests.test_chat_stream import _ask_stream
from tests.test_parse import _make_kb
from tests.test_upload import PG_AVAILABLE


# ── 测试夹具 ────────────────────────────────────────────────────────────────


def _doc_hit(chunk_id: str = "d1c1", score: float = 0.9, content: str = "工时八小时") -> dict:
    """向量命中（Document）。"""
    return {
        "id": chunk_id,
        "doc_id": "d1",
        "doc_name": "手册.pdf",
        "page_num": 3,
        "title_path": "考勤",
        "content": content,
        "score": score,
    }


def _kg_hit(score: float = 0.5, content: str = "当归 配伍 黄芪") -> dict:
    """KG 关系命中（阶段十三；字段口径对齐 kg_retrieval.retrieve 的输出）。"""
    return {
        "id": "kg:e1",
        "doc_id": "kg:e1",
        "content": content,
        "score": score,
        # BUG-064：KG 命中复用 dense_score 字段通过 Relevance Gate（量纲不同，已知限制）
        "dense_score": score,
        "source_kind": "kg",
        "resource_type": "herb",
        "resource_id": "r-1",
        "resource_name": "当归",
        "doc_name": "当归",
        "kg_relation": "compatible",
        "kg_hop": 1,
        "kg_provenance": "《本草纲目》",
    }


# ── BUG-068：系统管理员校验只有一份实现 ──────────────────────────────────────


def _fake_request(role: str = "member"):
    """构造带 request.state.user 的最小替身（require_admin 只读这两个属性）。"""
    from types import SimpleNamespace

    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(role=role)))


def test_require_admin_denies_non_admin():
    """非 admin 必须 403（PermissionDeniedError），文案由调用点决定。"""
    import pytest

    from src.core.deps import require_admin
    from src.core.exceptions import PermissionDeniedError

    with pytest.raises(PermissionDeniedError) as exc:
        require_admin(_fake_request("member"), "仅管理员可查看审计日志")
    assert "仅管理员可查看审计日志" in str(exc.value)


def test_require_admin_returns_user_for_admin():
    from src.core.deps import require_admin

    request = _fake_request("admin")
    assert require_admin(request, "任意文案") is request.state.user


def test_require_admin_denies_when_no_user_in_state():
    """request.state.user 缺失时不得放行（防止路由漏挂鉴权依赖后被绕过）。"""
    import pytest
    from types import SimpleNamespace

    from src.core.deps import require_admin
    from src.core.exceptions import PermissionDeniedError

    with pytest.raises(PermissionDeniedError):
        require_admin(SimpleNamespace(state=SimpleNamespace()), "x")


def test_admin_check_helpers_delegate_to_single_implementation():
    """BUG-068：各模块的管理员校验必须委托到 src.core.deps.require_admin。

    注意：herbs/prescriptions/theories/literatures/taxonomy 等资源路由仍有内联的
    `user.role != "admin"` 判断（AppException 403），本批按约定不动它们的业务复制，
    因此这里只校验本批收敛的四个模块的 helper 本体。
    """
    from pathlib import Path

    routes_dir = Path(__file__).resolve().parents[1] / "src" / "api" / "routes"

    for name in ("audit.py", "users.py", "settings.py"):
        source = (routes_dir / name).read_text(encoding="utf-8")
        assert "def _require_admin" in source, f"{name} 缺少 _require_admin"
        wrapper = source.split("def _require_admin", 1)[1].split("\n\n\n", 1)[0]
        assert "require_admin(request," in wrapper, f"{name} 的 helper 未委托到 core.deps"
        assert 'role != "admin"' not in wrapper, f"{name} 仍自己实现了 role 比对"

    # admin.py 原先是函数内的内联判断，已改为直接调用 require_admin
    admin_source = (routes_dir / "admin.py").read_text(encoding="utf-8")
    assert 'role != "admin"' not in admin_source
    assert 'require_admin(request, "仅管理员可查看系统统计")' in admin_source


# ── BUG-069：回收站保留天数单一数据源 ────────────────────────────────────────


def test_trash_retention_reads_runtime_config_not_hardcoded(monkeypatch):
    """三个资源模块都不再写死 7 天：统一走运行时配置入口（DB 覆盖 .env）。"""
    from pathlib import Path

    routes_dir = Path(__file__).resolve().parents[1] / "src" / "api" / "routes"
    for name in ("users.py", "knowledge_bases.py", "documents.py"):
        source = (routes_dir / name).read_text(encoding="utf-8")
        assert "get_system_value('trash_retention_days')" in source, (
            f"{name} 未统一读运行时配置"
        )
        assert "TRASH_RETENTION_DAYS = 7" not in source, f"{name} 仍写死 7 天"


def test_trash_retention_message_follows_config(monkeypatch, request):
    """改运行时配置后提示文案必须同步（BUG-069 的核心症状）。"""
    from src.core import runtime_config
    from src.core.config import settings

    from src.api.routes.users import BatchDeleteResult

    # 还原快照，避免影响后续用例
    request.addfinalizer(runtime_config.invalidate_system_config)

    def apply_days(days: int) -> None:
        """改动运行时配置快照（DB 覆盖 .env 的等效行为）。"""
        cfg = runtime_config.system_config_defaults()
        cfg["trash_retention_days"] = days
        runtime_config.reset_system_config_snapshot(cfg)

    monkeypatch.setattr(settings, "TRASH_RETENTION_DAYS", 3)
    apply_days(3)
    assert BatchDeleteResult(total=1, success=1).message == "已移入回收站，3 天内可恢复"

    monkeypatch.setattr(settings, "TRASH_RETENTION_DAYS", 14)
    apply_days(14)
    assert BatchDeleteResult(total=1, success=1).message == "已移入回收站，14 天内可恢复"


# ── BUG-070：仪表盘统计排除回收站数据 ────────────────────────────────────────


def test_admin_stats_excludes_soft_deleted():
    """统计必须与列表口径一致：deleted_at IS NULL。"""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "api" / "routes" / "admin.py"
    ).read_text(encoding="utf-8")

    stats_start = source.split("async def get_system_stats", 1)[1]
    assert stats_start.count("deleted_at.is_(None)") >= 2, "docs / kbs 计数都需排除回收站"


# ── BUG-071：batch_restore 预检不得全表加载 ──────────────────────────────────


def test_batch_restore_does_not_load_full_table():
    """预检必须按 id 精确查询（不得出现不带 where 的 select(User)）。"""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "api" / "routes" / "users.py"
    ).read_text(encoding="utf-8")

    batch = source.split("async def batch_restore_users", 1)[1].split("@router", 1)[0]
    assert "select(User))" not in batch, "仍在全表加载 User（含 hashed_password）"
    assert "User.id.in_(payload.user_ids)" in batch
    assert "deleted_at.is_(None)" in batch, "冲突检查需限定活跃用户"


# ── BUG-072：审计 IP 取值：默认不变，白名单内才认 XFF ─────────────────────────


class _FakeRequest:
    """模拟 Starlette Request 的最小子集：client + headers。"""

    def __init__(self, host: str, headers: dict[str, str] | None = None):
        from types import SimpleNamespace

        self.client = SimpleNamespace(host=host) if host else None
        self.headers = headers or {}


def test_client_ip_defaults_to_direct_peer(monkeypatch):
    """未配置可信代理 → 行为与改动前一致（直连 IP），不接受客户端伪造的 XFF。"""
    from src.core.config import settings
    from src.core.deps import get_client_ip

    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "")
    req = _FakeRequest("10.0.0.9", {"x-forwarded-for": "1.2.3.4"})
    assert get_client_ip(req) == "10.0.0.9"


def test_client_ip_trusts_xff_only_from_whitelisted_proxy(monkeypatch):
    """直连来源是可信代理时才取 XFF 最左跳。"""
    from src.core.config import settings
    from src.core.deps import get_client_ip

    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "172.18.0.2, 127.0.0.1")
    trusted = _FakeRequest("172.18.0.2", {"x-forwarded-for": "203.0.113.7, 172.18.0.2"})
    assert get_client_ip(trusted) == "203.0.113.7"

    untrusted = _FakeRequest("203.0.113.9", {"x-forwarded-for": "1.2.3.4"})
    assert get_client_ip(untrusted) == "203.0.113.9", "非可信来源不得采信 XFF"


def test_client_ip_without_xff_header_falls_back(monkeypatch):
    from src.core.config import settings
    from src.core.deps import get_client_ip

    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "172.18.0.2")
    assert get_client_ip(_FakeRequest("172.18.0.2")) == "172.18.0.2"


def test_client_ip_handles_missing_client():
    """无 client（如某些测试/内部调用）时返回空串，不得抛异常。"""
    from src.core.deps import get_client_ip

    assert get_client_ip(_FakeRequest("")) == ""


# ── BUG-067：迁移补齐漂移列，且 downgrade 不得删数据 ─────────────────────────


def test_bug067_migration_is_appended_after_previous_head():
    """不得重写历史：新迁移必须挂在原 head 之后，且 head 唯一。"""
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "alembic"))
    script = ScriptDirectory.from_config(cfg)

    # head 会随后续增量迁移推进；这里断言"唯一 head"以及"Batch 6 的迁移
    # 仍按原顺序挂在 Batch 5 之后"，而不是写死 head（否则每次新增迁移都要改）
    heads = script.get_heads()
    assert len(heads) == 1, f"应只有单一 head，实际：{heads}"
    revision = script.get_revision("c185a0406aa3")
    assert revision.down_revision == "f3a1c7d9b2e4", "必须追加在 Batch 5 的 head 之后"
    # 当前 head 必须是从 Batch 6 头一路追加下来的后代（不得重写历史）
    chain = [r.revision for r in script.walk_revisions()]
    assert "c185a0406aa3" in chain, "Batch 6 迁移必须仍在迁移链上"
    assert heads[0] == chain[0], "head 必须是迁移链末端"


def test_bug067_downgrade_does_not_drop_columns():
    """downgrade 必须是"不删数据"的空操作（开发库两列均有真实数据）。

    这里用源码守卫：一旦有人为了"对称性"补上 op.drop_column，测试立即失败，
    强制其走"显式删除迁移 + 数据备份"流程而不是悄悄删开发库的列。
    """
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "c185a0406aa3_add_legacy_drift_columns_progress_.py"
    )
    source = migration.read_text(encoding="utf-8")

    downgrade_body = source.split("def downgrade", 1)[1]
    assert "drop_column" not in downgrade_body, "downgrade 不得删除列（会丢失既有数据）"
    assert "drop_table" not in downgrade_body


# ── BUG-063：evidence_id 必须是稳定主键 ─────────────────────────────────────


def test_evidence_id_is_stable_across_source_index():
    """同一条命中在不同展示位次下必须得到同一个 evidence_id（BUG-063）。

    过去 fallback 是 f"{source_kind}:{source_id}:{source_index}"，重排后就变了，
    前端无法用它做去重 / React key。
    """
    hit = _doc_hit(chunk_id="")
    hit.pop("id")

    first = hit_to_evidence(hit, 1)
    last = hit_to_evidence(hit, 7)

    assert first["evidence_id"] == last["evidence_id"]
    assert first["source_index"] == 1 and last["source_index"] == 7, "source_index 仍应参与展示编号"


def test_evidence_id_distinguishes_chunks_without_chunk_id():
    """没有 chunk_id 时，同一来源的不同片段不得撞成同一个 evidence_id。"""
    a = _doc_hit(score=0.9, content="片段A")
    b = _doc_hit(score=0.8, content="片段B")
    a.pop("id")
    b.pop("id")

    assert hit_to_evidence(a, 1)["evidence_id"] != hit_to_evidence(b, 2)["evidence_id"]


def test_evidence_id_prefers_chunk_id():
    """有 chunk_id 时用 chunk_id（天然稳定且全局唯一）。"""
    assert hit_to_evidence(_doc_hit(chunk_id="c-42"), 3)["evidence_id"] == "c-42"


def test_stable_evidence_id_uses_content_not_index():
    """evidence_id 由「来源 + 内容摘要」构成：内容相同则 id 相同，内容不同则 id 不同。"""
    from src.application.evidence import stable_evidence_id

    same = stable_evidence_id("document", "d1", "同一段文本")
    other = stable_evidence_id("document", "d1", "另一段文本")

    assert same == stable_evidence_id("document", "d1", "同一段文本")
    assert same != other


# ── BUG-064：KG 证据编号与分组顺序的已知差异（锁定） ─────────────────────────


def test_kg_evidence_numbered_last():
    """KG 命中拼在心排序列末尾 → KG 的 source_index 总是最大的一批（当前行为）。"""
    from src.application.evidence import build_evidence

    ev = build_evidence([_doc_hit(score=0.9), _kg_hit(score=0.5)])

    assert [e["source_index"] for e in ev] == [1, 2]
    assert ev[-1]["source_kind"] == "kg"


def test_group_order_follows_max_score_not_source_index():
    """已知限制（BUG-064，本批不修）：分组按组内最高分降序，与 source_index 可能不一致。

    这里构造「KG 分更高但编号靠后」的场景，把这一不一致固化为断言：
    将来若要做"统一排序"的实验，本用例必须同步更新。
    """
    from src.application.evidence import group_evidence

    ev = hit_to_evidence(_doc_hit(score=0.4), 1), hit_to_evidence(_kg_hit(score=0.95), 2)
    groups = group_evidence(list(ev))

    assert [g["group_key"] for g in groups] == ["kg:herb", "document"], "分组按最高分降序"
    kg_evidence = [e for g in groups if g["group_key"] == "kg:herb" for e in g["sources"][0]["evidences"]]
    assert kg_evidence[0]["source_index"] == 2, "KG 编号仍在末尾：分组顺序与编号顺序不一致"


# ── BUG-065：非流式 Citation 不得丢失 KG 关系字段 ────────────────────────────


def test_citation_model_keeps_kg_relation_fields():
    """Pydantic 会静默丢弃未声明字段 → 必须在 Citation 上显式声明（BUG-065）。"""
    from src.api.routes.chat import Citation

    citation = Citation(**hit_to_evidence(_kg_hit(), 1))

    assert citation.kg_relation == "compatible"
    assert citation.kg_hop == 1
    assert citation.kg_provenance == "《本草纲目》"


def test_citation_model_kg_fields_are_none_for_non_kg_evidence():
    """非 KG 证据的三个 KG 字段保持 None（不影响旧客户端解析）。"""
    from src.api.routes.chat import Citation

    citation = Citation(**hit_to_evidence(_doc_hit(), 1))

    assert citation.kg_relation is None
    assert citation.kg_hop is None
    assert citation.kg_provenance is None


# ── BUG-066：SSE 协议稳定 + done.answer 权威性 ──────────────────────────────


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_stream_uses_only_documented_event_names(client):
    """BUG-066 决策：保持现有协议，**不新增 corrected 事件**。

    因此这里锁定"事件名集合"：出现任何新事件名（尤其是 corrected）都说明协议被改。
    """
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id, "考勤制度.txt")

    events = _ask_stream(client, "公司实行什么工时制度？", kb_id)
    names = [name for name, _ in events]

    assert names[0] == "start"
    assert names[-1] == "done"
    assert set(names) <= {"start", "citations", "delta", "done", "error"}
    assert "corrected" not in names


@pytest.mark.skipif(not PG_AVAILABLE, reason="PostgreSQL 未启动")
def test_stream_done_answer_is_authoritative(client):
    """done.answer 是 Reflection 之后的最终权威文本，且与落库内容一致（BUG-066）。"""
    kb_id = _make_kb(client)
    _upload_doc(client, kb_id, "考勤制度.txt")

    events = _ask_stream(client, "公司实行什么工时制度？", kb_id)
    done = [data for name, data in events if name == "done"][-1]

    assert done["answer"], "done 必须携带最终答案"

    messages = client.get(f"/api/v1/chat/conversations/{done['conversation_id']}/messages").json()
    assert messages[-1]["content"] == done["answer"], "落库正文必须等于 done.answer"

    delta_text = "".join(d["content"] for name, d in events if name == "delta")
    if delta_text != done["answer"]:
        # Reflection 改写了答案：done 事件必须给出 Reflection 决策，供前端解释"为何变了"
        assert done["reflection"] is not None
