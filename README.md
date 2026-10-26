# 中医药知识资源管理与智能问答系统

面向中医药知识学习与查询的知识资源管理平台：统一管理中药、方剂、中医理论、经典及现代文献，并提供基于 RAG 的智能问答、证据引用、知识图谱与评测能力。

系统首先是知识资源管理系统，AI 问答是增强模块，不提供医疗诊断、自动处方或面向具体患者的治疗方案。

## 核心能力

- 用户、角色、权限、审计与回收站
- 知识库、文档上传 / 解析 / 切片 / 向量化
- 中药、方剂、中医理论、文献的 CRUD、分类、标签与检索
- 知识数据导入中心、批次溯源、测试数据清理
- RAG 问答：HyDE、BGE-M3 Dense + Sparse、RRF、Reranker、Evidence Gate、Citation、多轮、SSE
- 研究增强：Query Analyzer、Dynamic Router、KG Retrieval、Self Reflection、多来源证据
- 评测：测试集、RAGAS 风格指标、实验归档、策略 / Gate / Reflection 消融

## 技术栈

| 层级 | 选型 |
| --- | --- |
| 前端 | Next.js 14 App Router + React 18 + Ant Design 5 + Zustand + TanStack Query |
| 后端 | Python 3.10+ / FastAPI + SQLAlchemy 2 async + Alembic |
| 存储 | PostgreSQL 15、Redis 7、Milvus 2.4（etcd + MinIO） |
| AI | BGE-M3、BGE-Reranker-v2-m3、OpenAI 兼容 LLM |
| 部署 | Docker Compose |

## 目录结构

```text
backend/
├── src/
│   ├── api/routes/       # FastAPI 路由
│   ├── core/             # 配置、鉴权、RBAC、异常、运行时配置
│   ├── domain/           # SQLAlchemy ORM 模型
│   ├── application/      # 文档、资源、RAG、Router、Gate、评测、KG、导入
│   ├── infrastructure/   # DB / Redis / Milvus / 模型 / 解析 / 存储
│   └── utils/            # 切片、RRF、检索工具
├── alembic/              # 数据库迁移
├── tests/                # pytest
└── requirements.txt      # 核心依赖；AI 依赖见 requirements-ai.txt

frontend/
├── src/app/              # App Router 页面
├── src/components/       # 布局、聊天、证据面板、管理组件
├── src/stores/           # Zustand
├── src/services/         # Axios / token
├── src/hooks/            # SSE、缩放
└── src/utils/            # 证据分组、翻译等
```

## 快速启动

### 方式一：Docker Compose（推荐）

```bash
cp .env.example .env
# 按需修改 .env
# 首次需要先构建本地模型镜像（约 4.3GB，仅需一次）
docker build -t biyesheji-models:latest backend/models-image
docker compose up -d --build
```

- 前端：http://localhost:3000
- 后端文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

如果本机没有模型镜像，也可以在构建后端时从 HF 镜像站下载：

```bash
docker build --build-arg MODELS_SOURCE=download -t biyesheji-backend backend
```

### 方式二：本地开发

```bash
# 1. 启动基础设施（PG / Redis / etcd / MinIO / Milvus）
docker compose up -d postgres redis etcd minio milvus

# 2. 后端：核心依赖 + AI 依赖（BGE-M3 / Reranker）
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt -r requirements-ai.txt
alembic upgrade head
uvicorn src.main:app --reload --port 8000

# 3. 前端
cd frontend
npm install
npm run dev
```

说明：

- 只安装 `requirements.txt` 时，默认 `flagembedding` 向量化不可用；本地完整问答需要 `requirements-ai.txt`。
- 未启动 PostgreSQL/Redis 时后端仍可启动，`/health` 标记 `degraded`；但问答、检索、导入需要完整依赖。
- 文档上传支持 PDF / DOCX / TXT / MD；Parquet 导入需要 `pyarrow`（已列入 `requirements.txt`）。

## 页面

- `/chat`：智能问答、知识库选择、多轮会话、引用证据
- `/documents`：文档上传、状态筛选、重新向量化、回收站
- `/kb`：知识库、成员、资源挂载、回收站
- `/herbs`、`/prescriptions`、`/theories`、`/literatures`：四类知识资源
- `/taxonomy`：分类与标签
- `/evaluation`：测试集、评估、实验对比
- `/users`：用户与回收站
- `/audit`：审计日志
- `/settings`：模型配置、向量状态、系统参数
- `/admin`：系统仪表盘
- `/admin/import`：知识数据导入中心
- `/login`：登录

## 测试

```bash
cd backend
python -m pytest tests -v

cd frontend
npm test
npm run build
npm run lint
```

## 质量门禁

- 评测答案正确度默认 ≥ 75% 才允许合入
- 后端修改跑相关 pytest，前端检查 lint / build
- RAG 核心链路修改后进行实际问答测试
- 详细规则见 [AGENTS.md](AGENTS.md)、[PRD.md](PRD.md)、[TECH_DESIGN.md](TECH_DESIGN.md)

## 数据持久化

Docker Compose 已把业务数据绑定到项目根目录 `docker-data/`：

```text
docker-data/
├── postgres/   # 用户、知识库、文档元数据、资源、会话、评测
├── redis/      # 会话缓存
├── etcd/       # Milvus 元数据
├── minio/      # Milvus 对象数据
├── milvus/     # 向量集合
└── uploads/    # 上传的原始文档
```

删除容器、重建镜像或执行 `docker compose down` 都不会丢失数据；只有手动删除
`docker-data/` 才会丢失。`/settings` 的“数据持久化”卡片会显示当前目录，并支持复制路径。

如果此前使用旧版命名卷（`*_postgres_data` 等），需要一次性把旧卷内容复制到
`docker-data/` 后再启动新容器：

```bash
# 先停止服务
docker compose down

# 逐个复制（卷名可用 docker volume ls 查看，前缀可能是项目目录名）
docker run --rm -v <旧postgres卷>:/from -v "$PWD/docker-data/postgres":/to alpine sh -c "cp -a /from/. /to/"
docker run --rm -v <旧uploads卷>:/from -v "$PWD/docker-data/uploads":/to alpine sh -c "cp -a /from/. /to/"
# milvus / minio / etcd / redis 同理
```

Windows PowerShell 把 `$PWD` 换成 `${PWD}`；如果不需要保留旧数据，可忽略此步骤。

## 安全边界（毕设部署）

本项目按“单机 / 内网答辩”场景设计，不是多租户 SaaS：

- 生产部署必须替换 `SECRET_KEY`、MinIO 凭据，并限制 PostgreSQL / Redis / Milvus 端口对外暴露。
- LLM API Key 保存在 `model_configs`，接口只返回脱敏值；数据库本身未加密，部署时需限制数据库访问。
- JWT 同时存 `localStorage` 和同源 cookie（供 Next middleware 路由守卫），不是 HttpOnly；应通过 CSP / 不引入不可信脚本降低 XSS 风险。
- 登录页“记住账号和密码”把凭据保存在本机 `localStorage`，公用电脑请取消勾选；生产环境应改用浏览器密码管理器或服务端会话。
- 禁止把本系统直接暴露到公网；如需公网部署，应另行设计 HttpOnly 会话、密钥托管、限流与 HTTPS。
