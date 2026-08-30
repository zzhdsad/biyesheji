# 基于 RAG 的企业级智能知识问答平台

企业将内部文档统一上传至平台，系统自动完成解析、切片、向量化与索引构建；员工通过自然语言提问，获得**严格来源于内部资料**、附引用来源的答案。

核心链路：文档上传 → 解析切片 → 向量化入库 → 混合检索（向量 + BM25）→ Rerank → LLM 生成 → 引用溯源。

## 技术栈

| 层级 | 选型 |
| --- | --- |
| 前端 | Next.js 14 (App Router) + React 18 + Ant Design 5 + Tailwind CSS + Zustand + React Query |
| 后端 | Python 3.10+ / FastAPI + LangGraph + SQLAlchemy 2.0 (async) |
| 存储 | PostgreSQL 15（元数据）、Milvus 2.4（向量）、Redis 7（缓存/队列） |
| 部署 | Docker Compose 一键启动 |

## 环境要求

- Python ≥ 3.10、Node.js ≥ 18.17
- 或仅安装 Docker（使用容器化部署）
- 模型服务：vLLM 部署 Qwen2.5-14B-Instruct-AWQ（OpenAI 兼容接口），BGE-m3 Embedding

## 快速启动

### 方式一：Docker Compose（推荐）

```bash
cp .env.example .env
docker compose up -d
# 前端 http://localhost:3000  后端文档 http://localhost:8000/docs
```

### 方式二：本地开发

```bash
# 1. 启动基础设施（仅 PG/Redis/Milvus）
docker compose up -d postgres redis etcd minio milvus

# 2. 后端
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
uvicorn src.main:app --reload --port 8000

# 3. 前端
cd frontend
npm install
npm run dev
```

> 未启动 PostgreSQL/Redis 时后端仍可运行，`/health` 会将对应组件标记为 `unavailable`，便于开发调试。

## 目录结构

```
backend/
├── src/
│   ├── api/routes/       # FastAPI 路由（health/documents/chat/kb/evaluation）
│   ├── core/             # 配置、日志、异常
│   ├── domain/           # ORM 模型（users/kb/documents/messages...）
│   ├── application/      # 用例服务
│   ├── infrastructure/   # 数据库/向量库/LLM 客户端
│   ├── graph/            # LangGraph 检索-生成流水线
│   └── utils/
├── tests/                # pytest 单元/集成测试
└── requirements.txt      # 重依赖见 requirements-ai.txt

frontend/
├── src/app/              # App Router 页面（/chat /documents /kb /admin）
├── src/components/       # 组件（原子设计）
├── src/stores/           # Zustand
├── src/services/         # API 调用（Axios）
└── src/types/            # 与后端 Schema 对应的 TS 类型
```

## 页面

- `/chat` 对话主界面：左侧知识库与历史会话，消息流 + 引用卡片，Enter 发送 / Shift+Enter 换行
- `/documents` 文档管理：上传、状态筛选、重新向量化、删除
- `/kb` 知识库管理：创建、公开/私有、成员权限
- `/admin` 系统仪表盘：文档/问答统计、RAGAS 评估面板

## 测试

```bash
cd backend
.venv\Scripts\python.exe -m pytest tests -v
```

## 质量门禁

- 合入 `develop` 前必须通过 RAGAS 评估：准确率 ≥ 75%（测试集 ≥ 30 条）
- 单元测试覆盖率目标 ≥ 80%
- 详见 [AGENTS.md](AGENTS.md)、[PRD.md](PRD.md)、[TECH_DESIGN.md](TECH_DESIGN.md)
