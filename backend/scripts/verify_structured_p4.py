"""第四阶段（关联结构化数据）后端验证脚本 —— 一次性验收工具，不属于 pytest 测试套件。

只在显式指定的隔离库上跑，绝不碰主库：
    VERIFY_DB_URL=postgresql+asyncpg://... kp_phase4_verify
Milvus 指向不可达地址 + 内存向量库：若关联流程误触 RAG，用例会直接暴露。
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'} | {name}{' | ' + str(detail)[:200] if detail else ''}")


def _dsn():
    url = os.environ.get("VERIFY_DB_URL")
    if not url:
        raise SystemExit("必须显式提供 VERIFY_DB_URL（隔离库），脚本拒绝猜测连接配置")
    return url


async def main():
    from src.application.structured_group_service import StructuredGroupService
    from src.core.exceptions import AppException
    from src.domain.models import (
        Document,
        FileFolder,
        KnowledgeBase,
        StructuredGroup,
        StructuredGroupFile,
        StructuredGroupRecord,
        StructuredParse,
        StructuredRecord,
        User,
        Base,
    )
    from sqlalchemy import delete, func, select

    engine = create_async_engine(_dsn(), poolclass=NullPool)
    from sqlalchemy.ext.asyncio import async_sessionmaker

    # 建表（隔离库可能是空库）
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    kb_id = uuid.uuid4()
    user_id = uuid.uuid4()
    doc_ids: list[uuid.UUID] = []

    async def add_file(session, name, rows_json):
        doc = Document(
            id=uuid.uuid4(), kb_id=kb_id, file_name=name, file_type="json",
            space="structured", parse_status="skipped", storage_path=f"/tmp/{name}",
            file_size=len(rows_json),
        )
        session.add(doc)
        parse = StructuredParse(
            doc_id=doc.id, kb_id=kb_id, status="success", detected_format="json",
            encoding="utf-8", fields=[], record_count=0,
        )
        session.add(parse)
        fields = [{"name": k, "type": "string"} for k in rows_json[0].keys()]
        parse.fields = fields
        parse.record_count = len(rows_json)
        for i, payload in enumerate(rows_json):
            session.add(
                StructuredRecord(
                    doc_id=doc.id, kb_id=kb_id, seq_no=i, row_no=i + 1,
                    record_label=f"{name}#{i}",
                    payload=payload,
                    knowledge_text="；".join(f"{k}：{v}" for k, v in payload.items()),
                    anomalies=[],
                )
            )
        return doc.id

    students = [{"学号": "S01", "姓名": "张三"}, {"学号": "S02", "姓名": "李四"}, {"学号": "S03", "姓名": "王五"}]
    courses = [{"课程编号": "C01", "课程名称": "高等数学"}, {"课程编号": "C02", "课程名称": "大学语文"}]
    enroll = [
        {"选课编号": "E1", "学号": "S01", "课程编号": "C01", "成绩": "90"},
        {"选课编号": "E2", "学号": "S01", "课程编号": "C02", "成绩": "85"},
        {"选课编号": "E3", "学号": "S02", "课程编号": "C01", "成绩": "78"},
        {"选课编号": "E4", "学号": "S09", "课程编号": "C01", "成绩": "60"},
        {"选课编号": "E5", "学号": "S03", "课程编号": "C99", "成绩": "55"},
        {"选课编号": "E6", "学号": "", "课程编号": "C02", "成绩": "70"},
    ]

    async with sessions() as s:
        uname = f"p4_{uuid.uuid4().hex[:8]}"
        s.add(User(id=user_id, email=f"{uname}@example.org", username=uname,
                   hashed_password="x", role="admin"))
        await s.flush()
        s.add(KnowledgeBase(id=kb_id, name=f"p4-verify-{uuid.uuid4().hex[:6]}", owner_id=user_id))
        await s.flush()
        d1 = await add_file(s, "学生表.json", students)
        d2 = await add_file(s, "课程表.json", courses)
        d3 = await add_file(s, "选课表.json", enroll)
        doc_ids = [d1, d2, d3]
        await s.commit()
    check("准备隔离数据（3 个结构化文件 + 解析结果）", len(doc_ids) == 3)

    # 1) 不完整文件组不入库
    async with sessions() as s:
        service = StructuredGroupService(s)
        try:
            await service.create_group(kb_id, "只有一个文件", [doc_ids[0]])
            check("单个文件不能建关联组（1 个文件 → 400）", False, "未抛错")
        except AppException as exc:
            check("单个文件不能建关联组（1 个文件 → 400）", exc.code == 400, exc.message)
        await s.rollback()

    # 2) 普通文档空间文件不能进组
    async with sessions() as s:
        doc = Document(id=uuid.uuid4(), kb_id=kb_id, file_name="普通.md", file_type="md",
                       space="document", parse_status="pending", storage_path="/tmp/a.md",
                       file_size=1)
        s.add(doc)
        await s.flush()
        plain_id = doc.id
        await s.commit()
    async with sessions() as s:
        service = StructuredGroupService(s)
        try:
            await service.create_group(kb_id, "混入普通文档", [doc_ids[0], plain_id])
            check("普通文档不能作为关联组成员 → 400", False, "未抛错")
        except AppException as exc:
            check("普通文档不能作为关联组成员 → 400", exc.code == 400, exc.message)
    async with sessions() as s:
        await s.execute(delete(Document).where(Document.id == plain_id))
        await s.commit()

    # 3) 建组 → draft → 手动填写主外键关系 → 校验 → 确认生成
    group_id = None
    async with sessions() as s:
        service = StructuredGroupService(s)
        group = await service.create_group(kb_id, "学生选课关联组", doc_ids)
        group_id = group.id
        check("多文件建组成功且状态为 draft", group.status == "draft" and group.record_count == 0)
        check("文件组成员按顺序落库", len(await service.list_files(group_id)) == 3)

    async with sessions() as s:
        service = StructuredGroupService(s)
        detail = await service.detail(group_id)
        cand = {f["file_name"]: [c["field"] for c in f["key_candidates"]] for f in detail["files"]}
        check("结构识别：每个文件都有字段与主键候选",
              all(cand.get(n) for n in ("学生表.json", "课程表.json", "选课表.json")), cand)
        # 主外键关系一律由用户手动填写：新建组没有任何预置关系
        g = await service.get(group_id)
        check("新建组关系为空，等待用户手动填写", list(g.relations or []) == [] and g.record_count == 0)

    rels = [
        {"from_doc_id": str(doc_ids[2]), "from_field": "学号", "to_doc_id": str(doc_ids[0]), "to_field": "学号",
         "kind": "many_to_one", "confirmed": True, "reason": "用户手动填写"},
        {"from_doc_id": str(doc_ids[2]), "from_field": "课程编号", "to_doc_id": str(doc_ids[1]), "to_field": "课程编号",
         "kind": "many_to_one", "confirmed": True, "reason": "用户手动填写"},
    ]
    async with sessions() as s:
        service = StructuredGroupService(s)
        await service.save_relations(group_id, rels)
        report = await service.validate(group_id)
        check("校验：主键/外键/孤立/类型问题有明细",
              report["error_count"] >= 1 and report["warning_count"] >= 1,
              [f"{i['type']}:{i['level']}" for i in report["issues"]])
        check("校验：外键无法匹配需要用户显式知情", report["force_required"] is True
              and report["unmatched_error_count"] >= 1)
        try:
            await service.confirm(group_id)
            check("未 force 时拒绝生成（409）", False, "未抛错")
        except AppException as exc:
            check("未 force 时拒绝生成（409）", exc.code == 409, exc.message)
        result = await service.confirm(group_id, None, True)
        check("确认后生成关联记录", result["record_count"] == 6, result)

    async with sessions() as s:
        service = StructuredGroupService(s)
        rows, total = await service.list_records(group_id, 20, 0, None, False)
        texts = {r.anchor_record_id: r.knowledge_text for r in rows}
        one = next(r for r in rows if (r.parts or [{}])[0]["values"].get("选课编号") == "E1")
        check("关联记录反映实体与关系（不是孤立的一行）",
              "张三" in one.knowledge_text and "高等数学" in one.knowledge_text and "90" in one.knowledge_text,
              one.knowledge_text)
        e4 = next(r for r in rows if (r.parts or [{}])[0]["values"].get("选课编号") == "E4")
        check("未匹配的外键原样保留并带提示（不丢行、不改值）",
              any("S09" in a for a in e4.anomalies) and "S09" in e4.knowledge_text, e4.knowledge_text)
        e6 = next(r for r in rows if (r.parts or [{}])[0]["values"].get("选课编号") == "E6")
        check("空外键行保留并提示", any("为空" in a for a in e6.anomalies), e6.anomalies)
        src = {(s_["file_name"], s_["role"]) for s_ in one.sources}
        check("来源追溯含文件组内各文件 + 行号/记录 id",
              {("选课表.json", "anchor"), ("学生表.json", "linked"), ("课程表.json", "linked")} <= src
              and all(s_["record_id"] for s_ in one.sources), src)
        check("来源追溯含参与关联的字段",
              all(s_["join_field"] for s_ in one.sources if s_["role"] == "linked"))
        check("记录总数与组统计一致", total == 6)
        check("组状态转 confirmed 且记录数落库", (await service.get(group_id)).status == "confirmed")

    # 4) 未经确认的关系不能入库
    async with sessions() as s:
        service = StructuredGroupService(s)
        g = await service.get(group_id)
        unconfirmed = list(g.relations) + [
            {"from_doc_id": str(doc_ids[0]), "from_field": "学号", "to_doc_id": str(doc_ids[1]),
             "to_field": "课程编号", "kind": "many_to_many", "confirmed": False, "origin": "suggested"}
        ]
        g.relations = unconfirmed
        await s.commit()
        try:
            await service.confirm(group_id, None, True)
            check("存在未确认关系时拒绝生成（400）", False, "未抛错")
        except AppException as exc:
            check("存在未确认关系时拒绝生成（400）", exc.code == 400, exc.message)
        async with sessions() as s2:
            keep = await s2.scalar(select(func.count()).select_from(StructuredGroupRecord)
                                   .where(StructuredGroupRecord.group_id == group_id))
            check("被拒绝的生成不影响已有正式记录", keep == 6, keep)

    # 5) 文件替换 → 组转 stale；成员删除 → 关系与记录清理
    async with sessions() as s:
        service = StructuredGroupService(s)
        ids = await service.mark_stale_by_docs([doc_ids[2]], "文件已替换，关联结果需重新校验")
        g = await service.get(group_id)
        check("组内文件替换 → 关联结果标记需重新校验",
              str(group_id) in [str(i) for i in ids] and g.status == "stale" and g.validation == {})

    async with sessions() as s:
        service = StructuredGroupService(s)
        g = await service.remove_file(group_id, doc_ids[1])
        check("移出成员后引用它的关系被清理（不留无效关系）",
              all(str(doc_ids[1]) not in (r["from_doc_id"], r["to_doc_id"]) for r in g.relations),
              g.relations)
        g2 = await service.remove_file(group_id, doc_ids[0])
        check("组内不足 2 个文件时正式记录作废", g2.record_count == 0 and g2.status == "stale")
        left = await s.scalar(select(func.count()).select_from(StructuredGroupRecord)
                              .where(StructuredGroupRecord.group_id == group_id))
        check("作废后不残留关联记录", left == 0, left)

    # 6) 彻底删除文件 → FK CASCADE 清理成员，其他组/文件不受影响
    async with sessions() as s:
        service = StructuredGroupService(s)
        other = await service.create_group(kb_id, "另一个组", [doc_ids[0], doc_ids[1], doc_ids[2]])
        await service.save_relations(other.id, [
            {"from_doc_id": str(doc_ids[2]), "from_field": "学号", "to_doc_id": str(doc_ids[0]),
             "to_field": "学号", "kind": "many_to_one", "confirmed": True}])
        await service.confirm(other.id, None, True)
        other_count = await s.scalar(select(func.count()).select_from(StructuredGroupRecord)
                                     .where(StructuredGroupRecord.group_id == other.id))
    async with sessions() as s:
        # 与 documents 路由的 purge 顺序一致：必须在删除 documents 行之前反查受影响的文件组
        ids2 = await StructuredGroupService(s).mark_stale_by_docs(
            [doc_ids[0]], "文件已彻底删除，关联记录已作废", True
        )
        await s.execute(delete(StructuredRecord).where(StructuredRecord.doc_id == doc_ids[0]))
        await s.execute(delete(StructuredParse).where(StructuredParse.doc_id == doc_ids[0]))
        await s.execute(delete(Document).where(Document.id == doc_ids[0]))
        await s.commit()
    async with sessions() as s:
        members = await s.scalar(select(func.count()).select_from(StructuredGroupFile)
                                 .where(StructuredGroupFile.doc_id == doc_ids[0]))
        g = await s.get(StructuredGroup, other.id)
        check("删除文件后成员行被级联清理", members == 0, members)
        check("删除文件后受影响组被标记且记录作废",
              g.status == "stale" and g.record_count == 0 and str(other.id) in [str(i) for i in ids2])
        still = await s.scalar(select(func.count()).select_from(StructuredRecord)
                               .where(StructuredRecord.doc_id == doc_ids[2]))
        check("其他文件的第三阶段独立记录不受影响", still == 6, still)

    # 7) RAG 隔离：documents.parse_status 未被改动，且没有任何切片
    async with sessions() as s:
        statuses = (await s.execute(
            select(Document.parse_status).where(Document.id.in_(doc_ids)))).all()
        from src.domain.models import Chunk
        chunks = await s.scalar(select(func.count()).select_from(Chunk)
                                .where(Chunk.doc_id.in_(doc_ids)))
        check("结构化文件 parse_status 仍为 skipped（RAG 隔离未被破坏）",
              all(r[0] == "skipped" for r in statuses), statuses)
        check("关联处理没有产生任何向量切片", chunks == 0, chunks)

    # 清理
    async with sessions() as s:
        await s.execute(delete(StructuredGroup))
        await s.execute(delete(StructuredGroupRecord))
        await s.execute(delete(StructuredGroupFile))
        await s.execute(delete(StructuredRecord))
        await s.execute(delete(StructuredParse))
        await s.execute(delete(Document))
        await s.execute(delete(FileFolder))
        await s.execute(delete(KnowledgeBase))
        await s.execute(delete(User))
        await s.commit()
    await engine.dispose()

    print(f"\n合计：通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
