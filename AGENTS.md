# AGENTS.md

> 项目：中医药知识资源管理与智能问答系统
> 更新时间：2026-10-20（已按当前代码核对）
> 作用：约束 AI Agent 的开发行为。
> 文档优先级：PRD.md（做什么）、TECH_DESIGN.md（怎么实现）、AGENTS.md（怎么改）。
> 文档与代码冲突时，先检查代码现状；改完代码后同步更新相关文档。

## 1. 文档优先级

- 开发前至少阅读 PRD、TECH_DESIGN、AGENTS 中与任务相关的部分。
- 不凭文档假设代码结构；以 `backend/src`、`frontend/src`、`alembic`、`tests` 的实际实现为准。
- 不为了让文档好看而修改代码行为；也不为了快速通过而删除已有功能。

## 2. 核心原则

- 系统主体是中医药知识资源管理，AI 是增强模块，不得改造成纯 AI Chat。
- 优先复用现有实现，最小修改；不重写已有模块，不为新功能删除旧功能。
- 不随意增加框架、服务或依赖；普通检索继续用 PostgreSQL，不引入 LLM、Milvus 或独立搜索引擎。
- 改动前先确定影响范围；多模块改动先拆步骤，小步验证，不一次重写整个项目。

## 3. 安全与权限

- 业务接口默认需要登录；免鉴权的业务端点只有 `/api/v1/auth/login` 与根路径 `/health`（FastAPI 的 `/docs`、`/openapi.json` 除外）。新增业务路由必须挂到 `protected_router`，不要绕过 `get_current_user`。
- 系统角色为 `admin` / `member` / `viewer`；当前接口主要区分 admin 与非 admin。管理端能力（用户、导入、审计、仪表盘、系统 / 模型配置写、KG 构建、四类资源写）仅 admin。
- 知识库权限必须使用 `get_accessible_kb_ids` / `require_kb_read` / `require_kb_write`：文档、评测写要求 `owner/admin/editor`；KB 元信息、成员、资源挂载 / 卸载要求 owner 或系统管理员。
- 不信任前端隐藏按钮；所有权限以服务端校验为准。禁止跨 KB 读取、检索、评测或导出。
- 管理员关键写操作应接入 `AuditService`；密钥 / API Key 不得明文返回前端，配置接口保持脱敏。
- 不开放自助注册；账号由管理员创建，首次登录必须改密。

## 4. RAG Baseline

除非用户明确要求，不得删除或破坏现有 RAG Baseline：

- HyDE 查询改写（默认开启，默认复用主 LLM）。
- BGE-M3 Dense + Sparse 检索。
- RRF 融合。
- BGE-Reranker 精排。
- 相似度门槛 / Evidence Gate。
- Citation / Evidence / 多来源分组。
- 多轮历史、Redis 缓存、PostgreSQL 回退。
- SSE 流式回答。

要求：

- 不重写 `rag_service` 的检索核心；新能力在 `retrieval_strategies`、`query_analyzer`、`dynamic_router`、`kg_retrieval`、`evidence_gate`、`self_reflection` 等模块上扩展。
- 保持降级语义：HyDE 失败回退原问题；Reranker 失败回退 RRF；KG 失败忽略；Gate / Reflection 异常安全放行或保留原答案。任何增强环节都不能成为问答单点故障。
- 新策略必须是 Baseline 的参数配置，不是另起一套 RAG；未知策略 / 异常一律回落 `baseline_hybrid`。
- 没有实验数据，不得声称某策略、Gate 或 Reflection 提升了效果。

## 5. 技术栈与传统业务

- 前端：Next.js App Router + React + Ant Design + Zustand + TanStack Query + Axios。
- 后端：FastAPI + SQLAlchemy 2 async + asyncpg + Alembic；分层为 `api/routes`、`core`、`domain`、`application`、`infrastructure`、`utils`。
- 路由只做鉴权、参数校验和编排；复杂业务放 `application`。简单 CRUD 可直接用 AsyncSession，但不要复制已有服务逻辑。
- 普通检索用 PostgreSQL `ILIKE` / `EXISTS`；不引入 Elasticsearch 等新服务。
- 数据库结构变更必须写 Alembic 迁移；生产不依赖 `create_all`，不得删库重建。删除优先软删除，并清理 Milvus 向量、Chunk、KB 挂载、KG 节点等派生数据。
- 配置优先级：`model_configs` / `system_configs` 的 DB 值优先于 `.env`。六个运行时参数（回收站、上传大小、Top-K、Top-N、拒答阈值、历史轮数）必须通过 `runtime_config.get_system_value()` 读取；模型配置通过 `ModelConfigService`，不要绕过统一入口。
- 生产链路禁止 mock / 假数据；mock 只允许在测试中使用。

## 6. AI 生成与来源约束

- 回答只依据系统知识库检索到的证据；证据不足时拒答或保守回答，不得用外部知识补答并伪装成知识库来源。
- Citation 必须对应真实 Chunk / 资源；禁止编造来源、页码、片段或引用编号。
- 来源字段（`source_type` / `era` / `credibility_level` / `source_dataset` / `import_batch_id`）必须保留；可信度等级由固定映射推导，不接受任意手工值。
- 英文原文的 AI 翻译必须标注“AI 翻译”，不得写回原文或冒充原始文献。
- 问题类型复用 `question_types.py` 受控词表，不另建字符串；分析 / 路由 / Gate / Reflection 的版本号、兜底原因和决策字段要保持可追溯。
- 禁止医疗诊断、自动处方、面向具体患者的治疗方案；Reflection 不得把 KG 弱关系扩展成医学结论。

## 7. 研究增强

以下功能已实现基础版，不是“未来规划”：

- Query Analyzer、EntityResolver、Dynamic Router、KG Retrieval、Evidence Gate、Self Reflection、多来源证据、评测与实验归档。

修改要求：

- 新能力模块化、可开关、可固定策略对比；不破坏 Baseline。
- 保持 `analyzer_version` / `router_version` / `gate_version` / `reflection_version` 等实验字段。
- 不把“未验证的假设”写成结论；评测、消融和对比必须使用真实数据。
- 涉及研究链路的改动，先跑相关单元测试，再做一次真实问答链路验证。

## 8. 数据与来源

- 保持 `Source → Document → Chunk` 可追溯；资源保留出处、来源数据集、导入批次；KG 边保留 provenance。
- 不伪造知识来源、实验数据、评测结果；未人工确认的标准答案不得按 0 分计入均值。
- 修改数据模型必须考虑已有数据、软删除、历史字段和迁移；不要直接改生产表结构。
- 资源 / KB 彻底删除时，按现有 `trash_service` / `cleanup_service` 生命周期清理向量、Chunk、挂载、KG 节点等派生数据。
- 导入数据缺失字段留空或标记 `pending`，不猜测、不翻译后冒充原文。

## 9. API 与前端兼容

- API 统一前缀 `/api/v1`；修改接口时检查请求参数、返回结构、错误处理和前端调用。
- 返回字段只做加法；旧 Citation 字段和已有响应结构保持兼容。
- SSE 事件名与顺序保持：`start → citations → delta×N → done`，异常用 `error` 代替 `done`；`done.answer` 是最终权威答案，`done.citations` 是最终引用（只含正文实际引用），前端须以 done 收敛引用列表。
- 流式接口需携带 Authorization；前端 `/api/v1/chat/ask-stream` 的 Route Handler 不能被 Next rewrites 取代（会缓冲 SSE）。
- 修改前端后检查 `src/services/api.ts`、`src/types`、`useSSE`、`chatStore` 和证据分组逻辑。

## 10. 医疗安全边界

系统仅用于中医知识学习与查询辅助。

禁止实现：

- 疾病诊断。
- 自动处方。
- 针对具体患者制定治疗方案。
- 替代医生进行医疗决策。

## 11. 测试与质量门禁

后端：

```bash
cd backend
python -m pytest tests -v
```

前端：

```bash
cd frontend
npm run lint
npm run build
npm test
```

要求：

- 后端改动优先跑相关 pytest；数据库 / Redis / Milvus 缺失时，集成测试可能跳过，不能把 skip 当成通过。
- RAG 改动做一次真实问答链路测试；前端改动检查 lint / build。
- 评测答案正确度默认 ≥ 75% 才允许合入；测试集建议 ≥ 30 条，且使用真实中医药数据。
- 不伪造测试通过、评测结果或实验数据。

## 12. Git 安全

禁止未经明确要求执行：

```bash
git reset --hard
git clean -fd
```

- 不覆盖或删除用户已有修改。
- 不提交密钥、模型权重、上传文件、运行日志和临时测试输出。
- 较大功能完成后建议提交 Git，但不擅自改写历史或强推。

## 13. 修改流程

1. 阅读相关文档和代码，确认实际实现。
2. 说明要改哪些文件、影响哪些模块。
3. 先做最小改动。
4. 跑相关测试 / lint / build。
5. 报告修改内容、API / DB 变化和测试结果。

禁止：未看代码就重构、大面积重写、删除已有功能、修改 API 后不查前端、改模型不写迁移。

## 14. 最终原则

先理解 → 再修改 → 再测试 → 再记录。

系统首先必须是完整、稳定的中医药知识资源管理系统，然后才是 AI 问答与研究增强。
