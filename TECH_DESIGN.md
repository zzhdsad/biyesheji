# 技术设计文档（TECH_DESIGN）

> 项目：中医药知识资源管理与智能问答系统
> 更新：2026-10-20（已按当前代码核对）
> 作用：说明系统技术架构、模块边界与核心实现。
> 产品需求见 PRD.md，开发规则见 AGENTS.md；本文只写技术设计。

## 1. 总体架构

```text
Browser
  → Next.js App Router（页面 / rewrites / SSE Route Handler）
  → FastAPI /api/v1
      ├─ PostgreSQL：业务事实源
      ├─ Redis：会话缓存（可选 Celery broker）
      ├─ Milvus：向量检索索引
      └─ LLM / Embedding / Reranker：OpenAI 兼容接口 + 本地 BGE
```

| 层 | 选型 |
| --- | --- |
| 前端 | Next.js 14.2 + React 18 + Ant Design 5 + Zustand + TanStack Query + Axios + Tailwind |
| 后端 | Python 3.12 + FastAPI + SQLAlchemy 2（async）+ asyncpg + Alembic |
| 数据 | PostgreSQL 15、Redis 7、Milvus 2.4.15（etcd + MinIO） |
| AI | BGE-M3（1024d dense + sparse）、BGE-Reranker-v2-m3、OpenAI 兼容 LLM、HyDE 复用主 LLM |
| 部署 | Docker Compose；模型权重随后端镜像内置，也支持构建期下载 |

约束：不使用 LangChain；生产链路不保留 mock，mock 仅测试使用；PG 是事实源，Milvus 是可重建索引。

## 2. 后端代码结构

| 目录 | 职责 |
| --- | --- |
| `src/api/routes` | HTTP 路由、Schema、SSE 输出 |
| `src/core` | 配置、运行时配置、JWT / 密码、依赖注入、RBAC、异常、日志 |
| `src/domain` | ORM 模型、关联表、受控常量 |
| `src/application` | 文档、资源、RAG、Router、Gate、Reflection、评估、KG、导入、清理 |
| `src/infrastructure` | DB、Redis、Milvus、Embedding、Reranker、LLM、HyDE、解析器、存储 |
| `src/utils` | 切片、RRF、检索辅助、时间 / 网络工具；`alembic` 迁移；`tests` pytest |

## 3. 数据存储

### 3.1 PostgreSQL

| 分组 | 表 |
| --- | --- |
| 用户 / 权限 | `users` |
| 知识库 | `knowledge_bases`、`kb_members`、`knowledge_base_resources` |
| 文档 | `documents`、`chunks`、`import_jobs`、`revectorize_jobs` |
| 知识资源 | `herbs`、`prescriptions`、`prescription_ingredients`、`theories`、`literatures` |
| 分类 / 标签 | `categories`、`tags` 与四类 `*_tags` 关联表 |
| 会话 / 评测 / KG | `conversations`、`messages`、`feedbacks`；`test_cases`、`evaluation_runs`、`evaluation_results`；`kg_nodes`、`kg_edges` |

历史兼容列：`documents.progress_percent`、`evaluation_results.faithfulness` 由迁移 `c185a0406aa3` 保留；ORM 已同步声明但业务不使用，避免 Alembic autogenerate 持续报 drift。
| 配置 / 审计 | `model_configs`、`system_configs`、`audit_logs` |

实现约定：

- 业务表统一 `created_at` / `updated_at`，纯关联表只保留 `created_at`；用户、KB、文档、资源、分类、标签用 `deleted_at` 软删除。
- 分类自引用 `parent_id`，同资源类型同父下名称唯一（表达式唯一索引）；标签名称全局唯一。
- `knowledge_base_resources` 用 `(knowledge_base_id, resource_type, resource_id)` 多态关联，不建跨表 FK。
- 评测快照 / 分组指标与审计详情使用 JSONB；标签与归经等使用 ARRAY。
- Alembic 管理迁移；开发环境 `init_db()` 用 `create_all`，只在 users 为空时 seed 默认管理员。

### 3.2 Redis

- 会话历史：`conversation:{conv_id}:history`，List（`{"role","content"}` JSON），TTL 24h。
- 每次追加 `LTRIM` 保留最近 `2*history_window` 条；PG 为事实源，未命中回源 PG 并回填；Redis 不可用时缓存降级。
- `PARSE_BACKEND=celery` 时 Redis 兼作 Celery broker/backend。

### 3.3 Milvus

Collection `document_chunks`：

- 标量：`id`(PK)、`doc_id`、`kb_id`、`chunk_index`、`content`、`page_num`、`title_path`。
- 向量：`dense_vector` 1024d（HNSW + COSINE）、`sparse_vector`（SPARSE_INVERTED_INDEX + IP）。
- 动态字段：`source_type`、`credibility_level`、`resource_type`、`resource_id`、`resource_name`、`era`、`embedding_model`。
- 文档向量 `doc_id` 为文档 UUID；资源向量 `doc_id` 为 `sha256(resource|type|id|kb_id)`，`resource_type` 非空。
- `replace_doc` 先写新向量、再删旧分片并校验条数；失败不空窗。

一致性：PG 保存文档 / Chunk 元数据与资源事实，Milvus 只做索引。删除、重建、切换 Embedding 都以 PG 状态为准；文档 `vector_model` 与 Milvus `embedding_model` 用于识别并排除旧模型文档向量。

## 4. 配置体系

- 环境配置：`src/core/config.py`（pydantic-settings）读取 DB / Redis / Milvus / JWT / 模型 / 切片 / 上传 / 存储 / 解析 / 导入 / Gate / Reflection 等配置；`ENV=prod` 禁止默认 `SECRET_KEY`。
- 模型配置：`model_configs` 单行表（id=1）保存 LLM、Embedding、Reranker、HyDE，DB 优先于 `.env`；`ModelConfigService` 合并、脱敏、更新，API Key 以 `****` 返回，写入后使进程缓存失效。
- 系统参数：`system_configs` 单行表（id=1）保存回收站保留期、上传大小、Top-K、Top-N、拒答阈值、历史轮数；`runtime_config.py` 用“启动预热 + 中间件 TTL=30s 刷新 + 写入立即失效”的快照，业务代码同步 `get_system_value()` 取值；DB 不可用时沿用旧快照或 `.env` 默认值。

## 5. 认证与权限

- 密码 bcrypt（rounds=10）；JWT HS256，payload 含 `sub/role/type=access/iat/exp`，默认 24h。
- `auth_public_router` 只暴露 `/auth/login`；`protected_router` 父级统一挂 `get_current_user` + `require_password_changed`；`/health` 为根路径免鉴权探针。
- `get_current_user` 校验签名、用户存在、未软删除、`is_active`，禁用账号返回 403。
- 系统角色 `admin/member/viewer`，当前接口主要区分 admin 与非 admin；KB 角色 `owner/admin/editor/viewer`，`get_accessible_kb_ids` 取 admin 全量、owner、成员、public 的并集。文档 / 评测写要求 `owner/admin/editor`；KB 元信息、成员、资源挂载 / 卸载要求 owner 或系统管理员。
- 用户、导入、审计、仪表盘、KG 构建、四类资源写操作仅 admin；模型 / 系统配置写仅 admin，读取对登录用户开放。
- `AuditService` 记录操作人、类型、目标、详情、IP、时间；CORS 由 `CORS_ORIGINS` 控制，错误由 `core/exceptions.py` 统一处理。

## 6. 文档处理与向量化

```text
upload → documents(pending) → parse(parsing) → chunks → BGE-M3(dense+sparse)
→ Milvus → documents(completed)
```

- 支持 PDF / DOCX / TXT / MD；大小走运行时配置；文件存本地或 MinIO，key 为 `{kb_id}/{doc_id}.{ext}`。
- 解析：Docling 为主，失败降级 pypdf / python-docx / 文本直读；扫描件无法提取文本时置 `failed` 并写 `error_message`。
- 切片：按 Markdown 标题层级分节，再按段落 / 句子滑动窗口切分；默认 768 tokens、overlap 100、min 32；CJK 约 1 字符 1 token，其他约 4 字符 1 token。Chunk 保存 `title_path`、`token_count`、来源类型与可信度；PDF 使用 pypdf 按页提取 1-based `page_num`（安装 Docling 时也优先页码），DOCX 无页码语义。
- 状态：`pending/parsing/success/failed/completed`；`success` 为切片就绪，`completed` 为向量已写入 Milvus。
- 异步：默认 FastAPI `BackgroundTasks`，`parse_runner` / `index_runner` 在新事件循环中执行并创建独立 DB engine，向量化全局信号量限流；`PARSE_BACKEND=celery` 时走 Celery（需另装并启动 worker）。
- 重新解析先删旧 Chunk 再重写；重新向量化用 `replace_doc` 幂等替换。
- 来源字段：`source_type/era/credibility_level` 冗余到 Chunk 与 Milvus，`credibility_level` 由来源类型固定映射；`backfill-source` 可按 doc_id / 文件名 / KB 补标，`completed` 文档触发重新向量化。
- 切换 Embedding 后由 `revectorize_jobs` 后台批量重建旧文档向量，进度落库，可重试 / 取消。

## 7. 资源挂载、向量化与导入

### 7.1 KB 资源挂载

- `knowledge_base_resources` 多态关联 KB 与 Herb / Prescription / Theory / Literature，同 KB 同资源唯一。
- 挂载时将资源字段拼为 Canonical Text（跳过空字段，不拼内部 ID）→ BGE-M3 → Milvus，向量行携带 `resource_type/resource_id/resource_name/era`。
- Herb / Prescription 通常 1 资源 1 向量；Theory / Literature 超过 6000 字符复用结构感知切片；向量 ID 与 doc_id 用 SHA-256 稳定生成。
- 挂载先写向量再提交关联，失败回滚；卸载先删向量再删关联。资源删除、KB 删除、彻底删除由 `trash_service` 清理挂载、Milvus 向量、KG 节点等派生数据。

### 7.2 导入中心

- `dataset_scanner` 扫描 `IMPORT_SOURCE_DIR`，支持 ZIP / TSV / CSV / JSON / JSONL / NDJSON / Parquet / TXT / MD；ZIP 只分析目录结构。
- 自动推断数据集类型、字段结构、样例与映射；无法可靠判断的字段标为 `pending`，导入时不写入。
- `import_jobs` 状态机 `pending → processing → completed / failed`，可 `cancelled`；记录 total / processed / succeeded / failed / skipped / failed_items。
- 文档导入复用 `DocumentService.upload → ParseService.run → IndexingService.run`；资源导入（中药 / 方剂 / 理论 / 文献）直接写业务表；均写入 `source_dataset` / `import_batch_id`。
- `upload_import_service` 提供上传预览与确认导入；`resource_vectorize` 支持批量挂载 + 向量化、断点续跑、重试、取消。
- `cleanup_service` 用 `import_batch_id` 区分真实数据与手工测试数据，先生成只读计划，确认后按“Milvus 向量 → chunks → 文件 → documents → 资源向量 → KB 挂载 → KG 节点 → 资源行”清理。

## 8. RAG 问答链路

### 8.1 总流程

```text
Query → QueryAnalyzer → EntityResolver → DynamicRouter → HyDE
→ BGE-M3 Dense + Sparse → Milvus → RRF → Reranker → 相关性门槛
→ Evidence 构建 / 分组 → Evidence Gate（可 retry 一次）→ LLM
→ Citation / Evidence → Self Reflection（非流式可 retry）
→ 持久化 + 缓存 + SSE
```

### 8.2 检索实现

- 入口：`POST /api/v1/chat/ask`（同步）与 `/chat/ask-stream`（SSE）；校验 KB 非空、KB 可访问、会话归属当前用户。
- `RetrievalStrategy` 解析 HyDE 开关、Dense / Sparse 路数、recall / rerank 条数、RRF `k`、`resource_type` 过滤、KG 开关与跳数。
- HyDE 默认开启，生成假设答案替换检索文本，最终 Prompt 仍用原问题；失败回退原问题。
- Dense 保留原始 `dense_score`（BGE-M3 COSINE），Sparse 用 BGE-M3 lexical weights，两路顺序执行（编码在线程池）；RRF `score(d)=Σ 1/(k+rank)`，`k` 默认 60，取 recall Top-K。
- Reranker 精排，不可用时回退 RRF 顺序；资源元数据优先从 PG 回填，老 Milvus 集合按 `resource_type` / `doc_id` 兜底。
- 相关性门槛只看 `dense_score`（不是 reranker 分数），最大值低于运行时阈值直接拒答；KG 证据仅在 `kg_enhanced` 等策略下合并，作为补充证据。

### 8.3 Evidence 与 Gate

- `build_evidence` 过滤低于相关性阈值的命中，按过滤后顺序生成连续 `source_index`；`displayable_hits` 保证 Prompt、Evidence、Citation 编号同源。
- 证据等级：`score >= 0.7` high，`>= 0.3` medium，否则 insufficient；展示分数用 rerank 优先，语义等级用 dense cosine 优先。
- 分组：`document`、`resource:<type>`、`kg:<type>`；组内按具体来源聚合，输出来源数 / 证据数 / 最高分 / 等级。
- Evidence Gate 位于 Rerank 后、LLM 前：`accept/insufficient/retry`，纯规则、不调用 LLM；最多换策略重试一次，仍不足则拒答；异常安全放行；Gate 关闭时返回 `None`，回到纯相关性门槛行为。

### 8.4 生成、引用与 Reflection

- Prompt = 系统约束 + 最近历史 + 带编号证据 + 用户问题；模型只依据证据回答，格式含 `[citation: 编号, 页码数字]`（无页码写 0）；模型误写「页码」占位词时仍按编号解析、页码以命中为准。Citation 从答案编号映射，越界 / 无法对应时不生成假引用。多子问题 Prompt 要求逐条回答，未知部分写「资料未涉及」，不因部分缺失而整体拒答。
- Gate retry 在 citations 下发前执行，同步 / 流式均可用；Reflection retry 仅非流式。流式路径生成前已下发 citations，只允许 `accept` / `revise`，最终答案以 `done.answer` 为准。
- Reflection 检查 Gate 拒答绕过、事实支撑、引用完整 / 越界、无依据句子、KG 弱关系扩展、多来源覆盖；结果 `accept/revise/retry`。默认规则优先，可选 LLM 检查默认关闭；revise / retry 各最多一次；异常保留原答案并记录兜底。
- 证据原文保持数据库文本；英文原文可调用 `/chat/evidence/translate` 生成 AI 中文翻译，译文标记为 AI 翻译，不写回原文。

### 8.5 持久化与 SSE

- 用户 / 助手消息写入 `messages`，引用 JSON 写入 `messages.citations`；Redis 追加历史并裁剪。
- SSE 顺序：`start → citations → delta×N → done`；异常用 `error` 替代 `done`。
- `start` 含 `conversation_id/user_message_id/query_analysis/router_decision`；`citations` 含 `citations/evidence/evidence_groups/evidence_summary/kg_evidence/evidence_gate`；`delta` 含 `content`；`done` 含 `conversation_id/message_id/answer/citations/evidence/evidence_groups/evidence_summary/kg_evidence/reflection`。`citations` 事件是生成前候选，`done.citations` 只含正文实际引用的来源。
- SSE 外层加注释帧心跳（默认 15s）和总超时（默认 900s）；同步接口返回同一套字段。
- 前端“停止”通过 AbortController 断开 fetch；服务端在下一次事件前检查 `request.is_disconnected()`，断开后不再生成/持久化完整回答。编辑提问调用 `DELETE /chat/conversations/{id}/messages/from/{message_id}` 截断该轮及之后消息，再以同一会话重新提问。

## 9. 研究增强模块

### 9.1 Query Analyzer 与实体消歧

- `QueryAnalyzer` 为确定性规则（`rule-v1`），不调用 LLM；输出 `question_type`、`keywords`、`entities`、`resource_types`、`is_multi_source`、`is_unanswerable_candidate`、`features`、版本与兜底原因。类型：`herb/prescription/theory/literature/multi_source/unanswerable/general`；异常 / 空问题回落 `general`。
- `EntityResolver` 用 PG 中真实资源 name / aliases 做内存索引（TTL 300s），判断问题是否出现确定资源名；消歧结果由 Analyzer 决定是否覆盖类型，随后可重跑 Router。

### 9.2 Dynamic Router 与策略注册表

| 策略 | 关键参数 |
| --- | --- |
| `baseline_hybrid` | 全部沿用全局配置，不限制资源 |
| `herb_focused` | `resource_types=("herb",)`，保留 Document，放大 recall / rerank |
| `prescription_focused` | `resource_types=("prescription",)`，保留 Document，放大 recall |
| `theory_focused` | `resource_types=("theory",)`，收紧 recall、调整 RRF |
| `literature_focused` | `resource_types=("literature",)`，关闭 HyDE，放宽 rerank |
| `multi_source` | 资源类型取 Analyzer 结果，保留 Document，放大 recall / rerank |
| `kg_enhanced` | Baseline 向量检索 + KG 关系证据，2 跳 |

- Router 只消费 `QueryAnalysis`，不重新解析原问题；规则版本 `rule-v2`；决策含 `strategy_name/reason/question_type/resource_types/router_version` 与生效参数。
- 未知策略 / 非法分析 / 异常回落 `baseline_hybrid`，策略名写入评测记录；顺序为“多来源优先 → 明显实体关系 KG → 单资源聚焦 → question_type 映射 → Baseline”。

### 9.3 知识图谱

- 图数据存 PG（`kg_nodes` / `kg_edges`），不引入图数据库；节点为四类资源，唯一 `(resource_type, resource_id)`，只存名称 / 别名，不复制正文。
- 边：`contains`（方剂 → 中药，来自 `prescription_ingredients`）、`records`（文献 → 资源，来自资源 `source` 命中文献名 / 别名）、`related_to`（共享标签，弱关系，有标签资源数 / 边数上限）。每条边保存 `provenance` 与 `description`；构建幂等，`rebuild=True` 单事务清空重建。
- 检索：实体 → 节点匹配 → BFS 最多 3 跳，跳数衰减 0.85；边两端都必须挂载在当前 KB；结果转为统一 Evidence（`source_kind=kg`）。`/kg/build`、`/kg/stats`、`/kg/relations`、`/kg/query`，构建仅 admin。

### 9.4 Evidence Gate

- `gate-v1`，输入统一 Evidence + QueryAnalysis + RouterDecision。
- 单条分类：document / resource 按 evidence level；KG 先看关系与跳数，`contains/records` 1 跳为强、2 跳为中等，`related_to` 或 ≥3 跳为弱且不接受。
- 聚合：只有中等证据需多条互证；纯 KG 需多条强关系；`unanswerable` 候选要求更多 high 证据与多来源。
- 决策 `accept/insufficient/retry`；retry 目标由 `RETRY_STRATEGY_BY_CURRENT` 确定性映射且不能等于当前策略。Gate 默认开启，异常安全放行，支持全局 / 评测开关。

### 9.5 Self Reflection

- `reflection-v1`，生成后执行；Reflection 默认开启，确定性检查为主，可选 LLM 一致性检查默认关闭。
- 检查：是否绕过 Gate 拒答、事实句是否有证据支持、引用编号是否完整 / 越界 / 对应、是否存在无依据句子、是否把 KG 弱关系扩展为医学结论、多来源是否只覆盖部分。
- 决策 `accept/revise/retry`；revise 基于现有证据重写，retry 换策略重检索；各最多一次。异常保留原答案并记录兜底；Gate 已拒答时只能接受；流式路径不执行 retry。

### 9.6 评测

- 数据：`test_cases`（问题、标准答案、标准上下文、question_type、dataset_version、needs_review、source_reference）；`evaluation_runs` 记录实验名、策略、版本、配置快照、聚合 / 分组指标；`evaluation_results` 记录逐条结果、检索上下文、Gate / Reflection 决策。
- 指标：RAGAS 风格 Context Relevancy（LLM 选相关句，失败回退 token 命中率）与 Answer Correctness（0.5 × 事实 F1 + 0.5 × Embedding 余弦，抽取失败回退字符 token F1）。
- 运行：固定策略或 `use_dynamic_router=True` 逐条路由；支持 `use_evidence_gate` / `use_self_reflection` 开关做消融；结果归档配置快照。
- 未确认标准答案的用例只算上下文相关度，答案正确度记 `None`；失败用例不计入均值。质量门禁默认 0.75；支持 `idempotency_key` 防重复运行。

## 10. API 设计

统一前缀 `/api/v1`；业务 API 除 `/api/v1/auth/login` 与根路径 `/health` 外均需登录。

| 前缀 | 职责 |
| --- | --- |
| `/auth` | 登录 / 退出 / 当前用户 / 改密 |
| `/users` `/audit` `/admin` | 用户管理、审计、仪表盘（admin）；`/admin/import` 导入中心 |
| `/kb` `/documents` | 知识库 / 成员；文档上传 / 解析 / 向量化 / 回收站 |
| `/chat` | `ask`、`ask-stream`、`evidence/translate`、会话管理 |
| `/kg` `/evaluation` `/feedbacks` | 知识图谱、评测、用户反馈 |
| `/settings` | 模型配置、Embedding 状态 / 重建、系统参数 |

兼容性：新增字段均为加法，旧 Citation 字段保持；SSE 事件名与顺序不变；`citations` 承载证据与 Gate，`done` 承载最终答案与 Reflection。`GET /documents` 支持可选 `limit` / `offset`，响应头 `X-Total-Count` 给出过滤后总数；不传 `limit` 仍全量返回。

## 11. 前端实现

- Next.js App Router；`AppLayout` + `AppSider` 统一布局。Zustand 管理会话 / 用户 / 文档 / 主题状态；TanStack Query 用于管理页数据；Axios 统一鉴权与错误处理。
- `next.config.mjs` 将 `/api/:path*` 与 `/health` rewrite 到 `BACKEND_URL`；`app/api/v1/chat/ask-stream/route.ts` 用 Route Handler 转发 SSE（避免 Next rewrites 缓冲），透传 Authorization 与状态码。
- `useSSE` 用 `fetch` POST + ReadableStream 解析事件；401 清 token 跳登录。证据面板优先用后端 `evidence_groups`，历史消息缺失时按 Citation 本地分组；相关度优先 dense `relevance_score`，缺失回退 score。
- 主题使用 Ant Design ConfigProvider + `themeStore`，默认科幻暗色，支持亮 / 暗 / 跟随系统；`EntryAnimation` 首次进入播放 CSS 过场；`useUiZoom` 提供 70%–160% 缩放按钮与 Ctrl + 滚轮。
- 问答支持 `AbortController` 停止流式生成；用户消息可“编辑后重问”，后端 `DELETE /chat/conversations/{id}/messages/from/{message_id}` 截断该轮及后续消息；`start` 事件返回 `user_message_id` 供前端精确定位。
- 引用卡片按 `source_kind` 生成“定位来源”链接：文档进 `/documents?doc_id=...`，资源按类型进对应列表并带 `keyword` 检索。
- 登录页支持记住账号密码（仅 localStorage，公共电脑应关闭）；系统设置展示 Docker 数据持久化目录。
- 回答正文按 Markdown 风格标题 / 列表 / 引用 / 加粗分块渲染，流式阶段由 `useTypewriter` 逐字展示，避免 delta 同帧到达时整段闪现。

## 12. 部署与运行

- `docker-compose.yml`：postgres、redis、etcd、minio、milvus、backend、frontend；持久化卷覆盖 PG / Redis / etcd / MinIO / Milvus / uploads。
- 后端镜像多阶段构建：Python 3.12；模型权重默认从本地 `biyesheji-models:latest` 镜像 COPY 到 `/app/models`（由 `backend/models-image/Dockerfile` 预构建），也支持 `MODELS_SOURCE=download`；镜像内包含 `alembic.ini` 与 `alembic/versions`，可执行迁移；运行时 `HF_HUB_OFFLINE=1`；CPU 编码限制 `OMP_NUM_THREADS=2`、`MKL_NUM_THREADS=2`。
- 数据持久化：PostgreSQL / Redis / etcd / MinIO / Milvus / uploads 均绑定到宿主机 `${HOST_DATA_DIR:-./docker-data}`，容器删除或重建不丢数据；`/settings` 的“数据持久化”卡片展示目录与备份说明。
- 前端构建时注入 `BACKEND_URL`（容器内 `http://backend:8000`），rewrites 在 build 阶段固化。
- FastAPI lifespan：初始化日志、开发环境 `init_db()`、预热运行时配置；生产库结构用 Alembic。`/health` 检查 PostgreSQL、Redis、Milvus（Milvus 1s 超时），返回 `status=ok/degraded` 与 `components`，进程存活仍为 HTTP 200。
- 测试：后端 `pytest`；前端 `node --test` 覆盖 stores / hooks / utils，另有 `next lint` 与 `next build`。`backend/alembic/versions` 保存基线（13 张核心表）与后续迁移。
