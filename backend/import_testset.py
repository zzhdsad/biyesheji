"""导入评测测试集到指定知识库（TASK-009 测试集录入能力）。

用法：

    python import_testset.py --kb-id <知识库UUID>
    python import_testset.py --kb-id <UUID> --file data/tcm_eval_seed_v1.json
    python import_testset.py --kb-id <UUID> --file my.json --dataset-version tcm-v2
    python import_testset.py --create-kb "中医评测测试集（tcm-v1）"（知识库不存在时自动创建）

说明：
- 直接使用 EvaluationService.save_test_set 写入 test_cases（不删除任何已有数据）。
- 支持两种文件格式：
  1) {"dataset_version": "tcm-v1", "cases": [...]}（种子文件，含元信息）
  2) [{...}, {...}]（纯数组，兼容旧的 sample_testset.json）
- 用例字段：question / question_type / golden_answer / golden_contexts /
  needs_review / source_reference / dataset_version。
- golden_answer 为空或 needs_review=true 的用例只计算 context_relevancy，
  answer_correctness 记为「未评估」，需人工确认标准答案后才参与统计。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections import Counter
from pathlib import Path

# Windows 控制台默认 GBK，中文输出需显式切到 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.application.evaluation_service import (  # noqa: E402
    QUESTION_TYPE_LABELS,
    EvaluationService,
)
from src.core.config import settings  # noqa: E402
from src.domain.models import KnowledgeBase  # noqa: E402
from src.infrastructure.database import get_session_factory  # noqa: E402

DEFAULT_FILE = Path(__file__).resolve().parent / "data" / "tcm_eval_seed_v1.json"


def _load_cases(path: Path) -> tuple[list[dict], str]:
    """读取测试集文件，返回 (cases, dataset_version)。"""
    with path.open(encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, dict):
        cases = payload.get("cases", [])
        version = str(payload.get("dataset_version") or "v1")
    elif isinstance(payload, list):
        cases, version = payload, "v1"
    else:
        raise SystemExit(f"不支持的文件格式：{type(payload)}")
    if not cases:
        raise SystemExit("测试集为空")
    return cases, version


async def _resolve_kb(db, kb_id: uuid.UUID | None, create_kb: str | None) -> KnowledgeBase:
    """确定目标知识库：--kb-id 优先；否则按 --create-kb 新建（已存在则复用）。"""
    if kb_id is not None:
        kb = await db.get(KnowledgeBase, kb_id)
        if kb is None:
            raise SystemExit(f"知识库不存在：{kb_id}")
        return kb
    if not create_kb:
        raise SystemExit("请提供 --kb-id 或 --create-kb")

    from sqlalchemy import select as _select

    from src.domain.models import User

    existing = (
        await db.scalars(_select(KnowledgeBase).where(KnowledgeBase.name == create_kb))
    ).first()
    if existing is not None:
        print(f"复用已有知识库：{existing.name}（{existing.id}）")
        return existing

    owner = (
        await db.scalars(_select(User).where(User.email == settings.DEFAULT_ADMIN_EMAIL))
    ).first()
    if owner is None:
        raise SystemExit(f"未找到默认管理员（{settings.DEFAULT_ADMIN_EMAIL}），请先初始化系统")
    kb = KnowledgeBase(
        name=create_kb,
        description="中医问答评测测试集所属知识库（TASK-009）",
        visibility="private",
        owner_id=owner.id,
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    print(f"已创建知识库：{kb.name}（{kb.id}）")
    return kb


async def _run(
    kb_id: uuid.UUID | None, path: Path, dataset_version: str | None, create_kb: str | None
) -> None:
    cases, file_version = _load_cases(path)
    version = dataset_version or file_version

    factory = get_session_factory()
    async with factory() as db:
        kb = await _resolve_kb(db, kb_id, create_kb)
        service = EvaluationService(db)
        ids = await service.save_test_set(kb.id, cases, dataset_version=version)

    counter = Counter(str(c.get("question_type") or "general") for c in cases)
    print(f"知识库：{kb.name} ({kb_id})")
    print(f"文件：{path}")
    print(f"数据集版本：{version}")
    print(f"本次导入：{len(ids)} 条")
    print("按问题类型分布：")
    for qtype, n in sorted(counter.items(), key=lambda kv: -kv[1]):
        print(f"  - {qtype}（{QUESTION_TYPE_LABELS.get(qtype, qtype)}）：{n}")
    pending = sum(1 for c in cases if c.get("needs_review", True))
    print(f"需人工确认标准答案：{pending} 条（未确认前不计入 answer_correctness）")
    print("完成：未删除任何已有数据。")


def main() -> None:
    parser = argparse.ArgumentParser(description="导入评测测试集")
    parser.add_argument("--kb-id", default=None, help="目标知识库 UUID（二选一）")
    parser.add_argument("--create-kb", default=None, help="知识库不存在时按此名称创建（二选一）")
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="测试集 JSON 文件路径")
    parser.add_argument("--dataset-version", default=None, help="覆盖数据集版本")
    args = parser.parse_args()

    kb_id: uuid.UUID | None = None
    if args.kb_id:
        try:
            kb_id = uuid.UUID(args.kb_id)
        except ValueError:
            raise SystemExit(f"非法 UUID：{args.kb_id}")
    if kb_id is None and not args.create_kb:
        raise SystemExit("请提供 --kb-id 或 --create-kb")

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"文件不存在：{path}")

    asyncio.run(_run(kb_id, path, args.dataset_version, args.create_kb))


if __name__ == "__main__":
    main()
