markdown
# AGENTS.md — AI 助手指引与开发规范

## 项目概述

本项目是一个基于 RAG 的企业级智能知识问答平台，包含：
- **后端**：Python FastAPI + LangGraph + Milvus + PostgreSQL
- **前端**：Next.js 14 + Ant Design + Tailwind
- **部署**：开发期采用本地运行模式，生产交付使用 Docker Compose

核心链路：文档上传 → 解析切片 → 向量化入库 → 混合检索 → Rerank → LLM 生成 → 引用溯源。

---

## 开发规范

### 分支策略
- `main`：生产稳定版，仅接受 PR
- `develop`：集成开发分支
- `feature/*`：新功能分支（如 `feature/hyde-optimize`）
- `fix/*`：缺陷修复分支

### 提交信息格式
<type>(<scope>): <subject>

[可选 body]
[可选 footer]

text
- type：`feat` / `fix` / `docs` / `style` / `refactor` / `test` / `chore`
- scope：`backend` / `frontend` / `deploy` / `docs`
- 示例：`feat(backend): add HyDE retrieval node`

### 代码评审要求
- 所有 PR 必须至少 1 人 approve
- 必须通过 CI（lint + test）
- 变更需更新对应文档（PRD / TECH_DESIGN / API 文档）

---

## 开发环境与运行模式（方案一：本地开发）

本项目采用**本地开发模式**，即前后端直接在宿主机运行，仅基础设施依赖容器化。

### 1. 基础设施（Docker 容器）

以下组件通过 Docker Compose 启动，保持后台运行：

```bash
# 仅启动基础设施（PostgreSQL + Redis + Milvus + MinIO）
docker compose up -d postgres redis milvus minio
组件	端口	用途
PostgreSQL	5432	业务数据存储
Redis	6379	缓存 + Celery Broker
Milvus	19530	向量存储与检索
MinIO	9000/9001	原始文件存储
2. 后端（本地 Python 环境）
bash
cd backend

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 复制环境变量
cp .env.example .env
# 编辑 .env，确保数据库连接指向 localhost:5432

# 启动后端（热加载模式，修改代码自动重启）
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
后端 API 文档：http://localhost:8000/docs

3. 前端（本地 Node.js 环境）
bash
cd frontend

# 安装依赖
npm install

# 启动前端（热加载模式，修改代码自动刷新）
npm run dev
前端访问地址：http://localhost:3000

4. Celery Worker（独立终端）
bash
cd backend
source venv/bin/activate
celery -A src.tasks.celery_app worker --loglevel=info
5. 禁止事项
禁止在开发期使用 docker compose up -d 启动后端或前端容器

禁止将本地开发修改直接推送到 main 分支

禁止在 .env 中硬编码敏感信息，必须使用 .env 注入

测试要求
单元测试
覆盖率目标 ≥ 80%

后端使用 pytest，前端使用 Jest + React Testing Library

关键模块（检索、切片、解析）必须有单元测试

运行测试
bash
# 后端测试
cd backend
pytest tests/ -v --cov=src --cov-report=html

# 前端测试
cd frontend
npm test
集成测试
API 端到端测试（pytest + httpx）

测试向量库写入/检索、LLM 调用 Mock

前端使用 Playwright 做核心 UI 流程测试

评估测试（质量门禁）
每次合并前必须运行 RAGAS 评估测试集（≥30 条）

准确率必须 ≥75% 才允许合入 develop

评估结果自动生成报告并附在 PR 中

代码风格
Python（后端）
格式化：black（line-length=100）

排序：isort

Lint：ruff（替代 flake8 + pylint）

类型注解：所有函数参数和返回值必须有类型注解

命名：snake_case 变量/函数，PascalCase 类名

运行 Lint
bash
cd backend
black src/ tests/
isort src/ tests/
ruff check src/ tests/
TypeScript（前端）
格式化：Prettier

Lint：ESLint（使用 @typescript-eslint）

命名：camelCase 变量/函数，PascalCase 组件/类，kebab-case 文件名（组件除外）

严格模式：strict: true

运行 Lint
bash
cd frontend
npm run lint
npm run format
注意事项（常见陷阱与约束）
文档解析
扫描件 PDF 可能 OCR 失败 → 必须提示用户上传可复制文本版本

超大文档（>50MB）解析超时 → 实现分片上传或限制

检索与生成
强制约束：System Prompt 必须包含“仅根据参考资料回答，找不到就说不知道”

禁止模型输出任何未在检索结果中出现的事实

引用标注必须在答案中显式显示，且必须可追溯

性能
大模型推理默认使用 AWQ 4bit 量化，确保单卡 24GB 可运行

检索超时设定：≤ 500ms（不含 LLM 生成）

异步处理：文档解析必须走 Celery，避免阻塞 API

安全
所有 API 需要鉴权（除 /health 外）

SQL 注入防范：使用 SQLAlchemy 参数化查询

知识库权限：检索时强制带 kb_id 过滤，禁止越权访问

环境配置
所有敏感信息（数据库密码、API Key）必须通过 .env 注入

禁止硬编码任何配置到代码中

提供 .env.example 模板

日志与可观测性
使用 Loguru（Python）和 pino（前端）记录结构化日志

关键链路（上传、解析、检索、生成）必须记录耗时和状态

错误信息必须包含堆栈（开发环境）或仅用户友好提示（生产环境）

交付标准
所有代码通过 lint + test

开发期本地运行（uvicorn --reload + npm run dev）正常

生产交付：Docker Compose 一键启动成功

准确率 ≥75%（通过内置评估）

API 文档（Swagger）自动生成且可访问

README 包含清晰的环境要求、启动步骤、示例数据

本地开发快速参考
组件	启动命令	访问地址
基础设施	docker compose up -d postgres redis milvus minio	—
后端	cd backend && uvicorn src.main:app --reload	http://localhost:8000
Celery	cd backend && celery -A src.tasks.celery_app worker --loglevel=info	—
前端	cd frontend && npm run dev	http://localhost:3000
关键文件说明
文件	用途
PRD.md	产品需求文档，所有功能设计的来源
TECH_DESIGN.md	技术设计文档，架构和选型的依据
AGENTS.md	本文档，AI 助手开发规范
docker-compose.yml	仅用于基础设施和生产部署
.env.example	环境变量模板