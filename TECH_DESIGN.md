# 技术设计文档（TECH_DESIGN）

> 项目：中医药知识资源管理与智能问答系统
> 更新：2026-11-11（按当前代码核对）
> 产品需求见 PRD.md，开发规则见 AGENTS.md。

## 1. 总体架构

```text
Browser
  → Next.js App Router（页面 / rewrites / SSE Route Handler）
  → FastAPI /api/v1
      ├─ PostgreSQL：业务事实源、文档/切片/结构化记录/会话
      ├─ Redis：会话历史缓存（可选 Celery broker）
      ├─ Milvus：dense + sparse 向量索引
      └─ BGE-M3 / BGE-Reranker / OpenAI 兼容 LLM
```

| 层 | 选型 |
| --- | --- |
| 前端 | Next.js 14.2 + React 18 + Ant Design 5 + Zustand + TanStack Query + Axios |
| 后端 | Python 3.12 + FastAPI + SQLAlchemy 2 async + asyncpg + Alembic |
| 数据 | PostgreSQL 15、Redis 7、Milvus 2.4.15（etcd + MinIO） |
| AI | BGE-M3（1024d dense + sparse）、BGE-Reranker-v2-m3、OpenAI 兼容 LLM、HyDE |
| 部署 | Docker Compose；模型权重随模型镜像内置，也支持构建期下载 |

约束：生产链路不使用 mock；mock 仅测试使用。PG 是事实源，Milvus 是可重建索引。

## 2. 后端代码结构

| 目录 | 职责 |
| --- | --- |
| `src/api/routes` | HTTP 路由、Schema、SSE 输出；文档、结构化、关联组、问答、设置、用户、审计 |
| `src/core` | 配置、运行时配置、JWT/密码、依赖注入、RBAC、异常、日志 |
| `src/domain` | ORM 模型、关联表、受控枚举（含历史资源表，当前运行时不写入） |
| `src/application` | 文档解析/索引、结构化解析/向量化、关联组关系生成、RAG、Router、Gate、Reflection |
| `src/infrastructure` | DB、Redis、Milvus、Embedding、Reranker、LLM、HyDE、解析器、存储 |
| `src/utils` | 切片、RRF、结构化关系/校验、时间/网络工具 |
| `alembic` | 迁移链；当前 head `a3d9f1c7e5b2`（删除 KG 表） |
| `tests` | 后端 pytest（隔离 kp_test / *_test 集合 / uploads_test） |

## 3. 数据存储

### 3.1 PostgreSQL

| 分组 | 表 |
| --- | --- |
| 用户/权限 | `users` |
| 知识库 | `knowledge_bases`、`kb_members` |
| 文档 | `documents`、`chunks`、`revectorize_jobs` |
| 目录 | `file_folders` |
| 结构化数据 | `structured_parses`、`structured_records` |
| 关联数据 | `structured_groups`、`structured_group_files`、`structured_group_records` |
| 会话 | `conversations`、`messages`、`feedbacks` |
| 配置/审计 | `model_configs`、`system_configs`、`audit_logs` |

`documents.space` 区分 `document` / `structured`；`structured_groups` 保存关系配置、校验状态、向量状态。历史资源表（`herbs` 等）保留在迁移中，但旧资源中心已下线，当前运行时不写入。

- 业务表统一 `created_at` / `updated_at`；用户、KB、文档等软删除。
- 迁移由 Alembic 管理；应用启动不 `create_all`，缺表提示执行 `alembic upgrade head`。
- 文档彻底删除按 `trash_service` / `cleanup_service` 清理 Chunk、向量、文件、挂载与派生数据。

### 3.2 Redis

- 会话历史：`conversation:{conv_id}:history`，List，JSON，TTL 24h。
- 每次追加 `LTRIM` 保留最近 `2*history_window` 条；PG 为事实源，未命中回源并回填。
- Redis 不可用时缓存降级，不阻断问答。

### 3.3 Milvus

Collection 默认 `document_chunks`：

- 标量：`id`、`doc_id`、`kb_id`、`chunk_index`、`content`、`page_num`、`title_path`。
- 向量：`dense_vector` 1024d（HNSW + COSINE）、`sparse_vector`（SPARSE_INVERTED_INDEX + IP）。
- 动态字段：`source_type`、`credibility_level`、`resource_type`、`resource_id`、`resource_name`、`era`、`embedding_model`。
- 文档向量 `doc_id` 为文档 UUID；结构化记录和关联组使用各自稳定的 doc_id；`resource_type` 非空用于旧资源兼容。
- `replace_doc` / `replace_docs_bulk` 先写新向量、再清旧分片并校验条数；失败不空窗。
- 检索排除 `vector_model` 不等于当前模型的旧向量；旧集合不支持动态字段时按“有向量即可用”降级。

## 4. 配置体系

- 环境配置：`src/core/config.py`（pydantic-settings），DB / Redis / Milvus / JWT / 模型 / 切片 / 上传 / 存储 / 解析 / Gate / Reflection；`ENV=prod` 拒绝弱 `SECRET_KEY`。
- 模型配置：`model_configs` 单行（id=1），DB 优先于 `.env`；`ModelConfigService` 合并、脱敏、更新，API Key 以 `****` 返回。
- 系统参数：`system_configs` 单行（id=1）保存回收站、上传大小、Top-K、Top-N、拒答阈值、历史轮数；`runtime_config` 启动预热 + 请求 TTL 刷新 + 写入失效；跨事件循环锁按 loop 分开。
- 测试隔离：`conftest.py` 在 `ENV=test` 时自动将上传目录/Milvus collection/MinIO bucket 切到 `*_test`；测试 DB 必须显式指向 `kp_test`，默认拒绝主库/开发库。

## 5. 认证与权限

- 密码 bcrypt；JWT HS256，payload 含 `sub/role/type=access/iat/exp`，默认 24h。
- 登录拒绝禁用/软删除用户；首次登录强制改密。
- 系统角色 `admin/member/viewer`；KB 角色 `owner/admin/editor/viewer`。
- `get_current_user` 挂 `request.state.user`；`get_accessible_kb_ids` 做 owner + members + public 并集。
- 文档/结构化/评测（已删除）/RAG/KG（已删除）均不得绕过 KB 权限。
- 测试鉴权开关仅允许 `ENV=test/testing`；dev/prod 不生效。

## 6. 文档处理与向量化

```text
上传 → 保存原文件 → documents(pending)
     → parse_service：解析文本/结构 → chunks
     → indexing_service：BGE-M3 dense+sparse → Milvus
     → documents(completed, index_status=indexed, vector_model)
```

- 解析：PDF（pypdf）/ DOCX / XLSX / PPTX / TXT / MD / CSV / TSV / 图片；Docling 可选，失败回退内置解析器。
- 切片：结构感知，默认 768 tokens、overlap 100、min 32；Chunk 保留页码和标题路径。
- 文档更新：原子替换文件 → 删除旧向量 → 删旧 Chunk → 重新解析/向量化；失败保留原文件并暴露状态。
- 文档删除：软删除后检索过滤；彻底删除清理 Milvus/Chunk/文件/挂载/KG 派生数据（KG 已下线）。
- 状态必须真实：`parse_status`、`index_status`、`vector_count`、`error_message` 与前端展示一致。
- CPU 向量化优化：BGE-M3 按本批文本长度动态选择 `max_length`（上限 1024），默认 4 线程、编码批 64，并对 CPU Linear 层启用 int8 动态量化（可用 `EMBEDDING_QUANTIZE=false` 回退全精度）。
- 断点续跑：停止时保留已写入向量和 `vector_count`，状态回到 `pending` 并写“已停止向量化，可继续”；下一次点击按已写入前缀跳过，不再从 0 开始。仅“上次停止”的 pending 计数会被当作断点，完成后重新向量化仍走整份重建。

## 7. 结构化数据与关联数据

### 7.1 单文件结构化

```text
上传(space=structured) → 解析字段/记录 → structured_parses/records
  → 生成统一知识文本 → BGE-M3 → Milvus → 统一检索
```

- 支持 CSV/TSV/XLSX/JSON；字段识别含类型、空值、记录数、主键候选。
- 记录保留原文件、sheet/json path、行号，支持追溯。
- 向量状态 `pending/indexing/completed/failed`；失败可重试，不影响解析结果。

### 7.2 关联数据组

```text
建组（≥2 文件）→ 解析字段 → 关系推荐/人工配置
  → 逐条确认 → 校验（主键/外键/匹配/孤立/类型）
  → 生成关联记录 → 向量化 → 统一 RAG
```

- 关系推荐：字段名、主键候选、样本值重叠打分，输出 `confidence/score/tier/reason`；建议默认未确认。
- 关系只能连接两个不同文件；保存时整份替换；校验报告作废旧结果。
- 关联知识文本由锚点记录 + 关系路径构成，必须包含关联信息，不能只拼接单表。
- 向量化完成才允许检索；关联组失败/未确认不留下可检索向量。
- 支持重新生成、重新向量化、取消、失败重试；取消保留已完成批次，下一次继续，避免大文件从头重跑。

## 8. RAG 问答链路

### 8.1 总流程

```text
question
 → Query Analyzer（类型/实体/意图）
 → Entity Resolver（名称消歧，失败保持原分析）
 → Dynamic Router（baseline / 聚焦 / multi_source；KG 已下线）
 → HyDE（失败回退原问题）
 → Dense + Sparse 召回
 → RRF 融合
 → BGE-Reranker 精排
 → 相关性阈值 + Evidence Gate（accept / insufficient / retry）
 → LLM（只依据证据）
 → Citation 构建 + Self Reflection
 → SSE start → citations → delta×N → done
```

### 8.2 检索

- Dense：BGE-M3 稠密向量余弦；Sparse：lexical weights。
- RRF：`score = Σ 1/(k+rank)`，默认 `k=60`。
- Top-K=10、Top-N=3；默认拒答阈值 0.35。
- CPU 问答延迟控制：RRF 仅前 `RERANK_MAX_CANDIDATES`（默认 10）送 Cross-Encoder，Reranker 按证据长度动态 `max_length` 并启用 int8 动态量化；启动时后台预热 BGE/Reranker，避免第一个提问承担模型加载成本。
- 按 KB 权限强过滤；旧模型向量排除；无法确认来源的命中降级。
- Router 失败 / 未知策略回落 `baseline_hybrid`。

### 8.3 Evidence Gate

- 生成前判断证据是否充分：`accept` / `insufficient` / `retry`。
- 区分 document / structured / structured_group 来源；不把不同来源分数直接混为同一量纲。
- 只有中等证据时要求多条互证；疑似超出知识库范围更严格；最多重试一次且策略不同于当前。
- Gate 异常安全放行；支持全局/评测（evaluation 已删除）开关，保留运行时开关。

### 8.4 生成、引用与 Reflection

- LLM 只接收问题 + 检索证据；无证据不生成确定性回答。
- 结构化证据字段覆盖检查：问题明确问功效/剂量/组成等，而命中记录只有名称、给药途径或术语关联时，确定性拒答，不让 LLM 把关联信息拼成答案；拒答不展示引用。
- Citation 只来自真实命中；`done.citations` 只含正文实际引用，不能编造编号/页码。
- Self Reflection 检查绕过 Gate、无依据句、引用越界/缺失、多来源覆盖；`accept/revise/retry` 各有限次。
- Reflection 异常保留原答案；流式路径不因 retry 改变已下发证据。

### 8.5 SSE 与持久化

- 事件顺序：`start → citations → delta×N → done`；异常用 `error` 代替 `done`。
- `done.answer` 是最终权威答案；前端以 done 收敛引用。
- 客户端断开时后端停止生成，不写孤儿 assistant 消息。
- 会话/消息写 PG；历史缓存写 Redis；引用 JSON 存 messages。

## 9. 研究增强

### 9.1 Query Analyzer / Entity Resolver

- 受控 `question_types`：herb / prescription / theory / literature / multi_source / unanswerable / general。
- 实体消歧只读资源名称/别名；通用词（功效、主治等）不作为命名实体。
- 索引缓存绑定 `DATABASE_URL`，切库强制失效。

### 9.2 Dynamic Router

- 策略是 Baseline 参数配置，不是另起 RAG；未知策略回落 baseline。
- 当前策略：baseline / herb_focused / prescription_focused / theory_focused / literature_focused / multi_source；`kg_enhanced` 已删除。
- 决策写入响应 `router_decision`，可解释、可回落。

### 9.3 Gate / Reflection

- 纯规则优先，LLM 一致性检查默认关闭。
- 版本号写入响应，决策可追溯；异常安全降级。

## 10. API 设计

统一前缀 `/api/v1`；除 `/auth/login` 与 `/health` 外均需登录。

| 前缀 | 职责 |
| --- | --- |
| `/auth` | 登录/退出/当前用户/改密 |
| `/users` `/audit` `/admin` | 用户、审计、仪表盘（admin） |
| `/kb` | 知识库、成员、回收站 |
| `/documents` | 普通文档/结构化文件上传、CRUD、预览、内容保存、替换、解析、向量化、回收站 |
| `/folders` | 文件/结构化空间文件夹 |
| `/structured` | 单文件结构化解析、记录、重解析、重新向量化 |
| `/structured/groups` | 关联组、成员、关系保存/推荐/确认、校验、生成、向量化 |
| `/chat` | ask、ask-stream、证据翻译、会话管理 |
| `/feedbacks` | 用户反馈 |
| `/settings` | 模型配置、Embedding 状态/重建、系统参数 |

兼容性：新增字段走加法；旧 Citation 字段保留；SSE 事件名/顺序不变；`GET /documents` 支持 `limit/offset` 与 `X-Total-Count`。

## 11. 前端实现

- `AppLayout` + `AppSider`；页面：chat / documents / kb / users / audit / settings / admin / login。
- 文档页统一普通文档与结构化数据；文件夹、上传、预览、替换、关联组抽屉。
- 关联组：字段/关系配置、自动推荐 + 置信度、逐条确认/忽略、校验报告、生成、向量化状态。
- 聊天：SSE、停止生成、编辑后重问、证据分组、引用定位、多轮。
- 主题：科幻暗色默认，亮/暗/跟随系统，70%–160% 缩放。
- 登录“记住账号密码”仅本机 localStorage，公用电脑应关闭。
- 旧 `/taxonomy`、`/admin/import`、资源中心页面已删除。

## 12. 部署与运行

- `docker-compose.yml`：postgres、redis、etcd、minio、milvus、backend、frontend；数据绑定 `${HOST_DATA_DIR:-./docker-data}`。
- 后端多阶段构建：Python 3.12；模型权重默认从 `biyesheji-models:latest` COPY 到 `/app/models`；也可 `MODELS_SOURCE=download`；包含 `alembic.ini`/`alembic`。
- 前端构建注入 `BACKEND_URL`（容器内 `http://backend:8000`）；SSE Route Handler 保留 Authorization。
- FastAPI lifespan：日志、开发环境 `init_db()`、运行时配置预热、结构化向量遗留状态恢复。
- `/health` 检查 PG / Redis / Milvus，返回 `ok/degraded`；进程存活仍 HTTP 200。
- 测试：后端 pytest；前端 `node --test`、`next lint`、`next build`；测试必须使用隔离 DB/集合/目录。
