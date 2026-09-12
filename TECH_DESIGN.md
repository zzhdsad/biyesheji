# 技术设计文档（TECH_DESIGN.md）— 精简版

## 1. 技术栈选择

| 层级 | 选型 | 简要理由 |
| :--- | :--- | :--- |
| **前端框架** | Next.js 14 (App Router) + React | SSR、API Routes、企业级 |
| **UI 组件** | Ant Design 5 + Tailwind CSS | 后台成熟、定制灵活 |
| **状态管理** | Zustand + React Query | 轻量全局 + 服务端缓存 |
| **后端框架** | Python 3.10+ / FastAPI | 异步、自动文档、AI 生态 |
| **RAG 编排** | 自研状态化 RAG 编排（HyDE → BGE-M3 稠密/稀疏混合检索 → RRF 融合 → BGE-Reranker 精排 → 相关性拒答 → 生成） | 有状态复杂流程（HyDE/多路召回），不依赖第三方编排框架，核心实现见 application/rag_service.py |
| **大模型** | Qwen2.5-14B-Instruct (AWQ 量化) | 中文 SOTA，单卡 24GB 可跑 |
| **Embedding** | BAAI/bge-m3 | 稠密+稀疏向量，检索精度高 |
| **Rerank** | BAAI/bge-reranker-v2-m3 | 精排提升准确率 |
| **向量数据库** | Milvus 2.4 | 分布式、混合检索、高可用 |
| **关系数据库** | PostgreSQL 15 | 元数据、用户、会话 |
| **缓存/队列** | Redis 7 (Celery broker) | 异步任务 + 会话缓存 |
| **文档解析** | Docling (IBM) + PaddleOCR | 表格/OCR 强，降级方案 |
| **部署** | Docker Compose + Nginx | 一键私有化部署 |
| **评估** | RAGAS（中文适配） | 自动化质量评估 |

---

## 2. 项目结构
knowledge-platform/
├── backend/
│ ├── src/
│ │ ├── api/routes/ # FastAPI 路由（documents, chat, kb, eval）
│ │ ├── core/ # 配置、常量、异常
│ │ ├── domain/ # 业务实体（Document, Chunk, KB...）
│ │ ├── application/ # 用例服务（上传、问答、评估）
│ │ ├── infrastructure/ # 向量库、LLM、解析器、DB、缓存
│ │ └── utils/ # 日志、切分工具（RAG 编排在 application/rag_service.py）
│ ├── tests/
│ └── Dockerfile
├── frontend/
│ ├── src/app/ # Next.js App Router（chat/documents/kb/admin）
│ ├── src/components/ # 原子/分子/页面组件
│ ├── src/hooks/ # useChat, useDocuments
│ ├── src/stores/ # Zustand stores
│ ├── src/services/ # API 调用（Axios）
│ └── Dockerfile
├── docker-compose.yml
└── .env.example

text

---

## 3. 数据模型

### PostgreSQL（核心表）
- **users**：id, email, username, hashed_password, role
- **knowledge_bases**：id, name, description, visibility, owner_id
- **kb_members**：(kb_id, user_id), role
- **documents**：id, kb_id, file_name, parse_status, chunk_count
- **conversations**：id, user_id, title, kb_ids[]
- **messages**：id, conversation_id, role, content, citations (JSON)
- **test_cases**：id, kb_id, question, golden_answer, golden_contexts
- **evaluation_results**：id, test_case_id, retrieved_contexts, context_relevancy, answer_correctness
- **feedbacks**：id, message_id, rating, comment

### Milvus（向量集合）
**Collection: `document_chunks`**
- `id`, `doc_id`, `kb_id`, `chunk_index`, `content`, `page_num`, `title_path`
- **`dense_vector`** (FLOAT_VECTOR, 1024d) — BGE-m3 稠密向量
- **`sparse_vector`** (SPARSE_FLOAT_VECTOR) — BGE-m3 稀疏向量
- 索引：dense→HNSW, sparse→SPARSE_INVERTED_INDEX

### Redis
- `session:{user_id}` → 用户会话（7d TTL）
- `conversation:{conv_id}:history` → 最近 N 轮消息（24h TTL）
- Celery 任务队列 + 限频计数

---

## 4. 关键技术点

| 技术难点 | 解决方案 |
| :--- | :--- |
| **文档解析鲁棒性** | Docling 为主 + PyPDF2/docx 降级；失败支持重试 |
| **语义切片粒度** | 结构感知（按标题） + 递归切分（512~1024 tokens, overlap 50-100） |
| **混合检索+重排** | 多路召回（稠密+稀疏）→ RRF 融合 → bge-reranker 精排 → 标量过滤（kb_id） |
| **多轮对话上下文** | 历史窗口（最近3-5轮）+ 查询改写（用历史摘要生成检索 Query） |
| **HyDE 实现** | 小模型（1.5B）生成假设答案 → 检索 → 主模型生成最终回答（可配置开关） |
| **引用溯源** | Prompt 强制标注 `[citation: doc_id, page]`；后处理匹配；前端渲染引用卡片 |
| **防幻觉** | System Prompt 强约束：“仅根据资料回答，找不到就说不知道”，并检查引用 |
| **异步文档处理** | 上传后 Celery 任务异步解析 → 状态轮询/SSE 推送进度 |
| **权限隔离** | RBAC（Admin/Owner/Editor/Viewer），所有检索加 kb_id 过滤，API 中间件鉴权 |
| **私有化交付** | Docker Compose 一键启动，离线模型打包，数据持久化挂载，备份恢复脚本 |
| **性能优化** | Milvus HNSW 索引；vLLM 连续批处理；Nginx 负载均衡（水平扩展） |
| **质量评估** | RAGAS 指标（Context Relevancy / Answer Correctness / Faithfulness），支持测试集批量运行 |

---

## 附：快速启动命令

```bash
git clone <repo>
cp .env.example .env
docker compose up -d          # 启动 PostgreSQL + Milvus + Redis + 模型
cd backend && pip install -r requirements.txt && uvicorn src.main:app --reload
cd frontend && npm install && npm run dev
# 访问 http://localhost:3000
```