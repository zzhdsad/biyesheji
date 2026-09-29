# syntax 指令不写：本机拉取 docker/dockerfile:1 前端需要访问 Docker Hub（不可达），
# 使用 BuildKit 内置前端即可支持 RUN --mount=type=cache。

# 模型权重的取得方式：
#   local    （默认）COPY backend/prebuilt-models —— 离线、稳定、最快
#   download 构建期从 HF 镜像站下载（需要外网约 4.3GB，见下方 models-download 阶段）
ARG MODELS_SOURCE=local

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

# ── 阶段 1：Python 依赖 ──────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime-base
# 多阶段里全局 ARG 必须在每个 stage 内重新声明，否则展开为空
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com
WORKDIR /app

# 核心运行时依赖（fastapi/sqlalchemy 等），PyTorch 走 CPU-only 索引减体积
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i ${PIP_INDEX_URL} \
    --trusted-host ${PIP_TRUSTED_HOST} \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    --timeout 300 --retries 10

# AI 重依赖：FlagEmbedding 同时覆盖 BGE-M3（向量化）与 BGE-Reranker-v2-m3（精排）
COPY requirements-ai.txt .
RUN pip install --no-cache-dir -r requirements-ai.txt \
    -i ${PIP_INDEX_URL} \
    --trusted-host ${PIP_TRUSTED_HOST} \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    --timeout 300 --retries 10 \
  || pip install --no-cache-dir FlagEmbedding sentence-transformers \
    -i ${PIP_INDEX_URL} \
    --trusted-host ${PIP_TRUSTED_HOST} \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    --timeout 300 --retries 10

# ── 阶段 2a：本机预置权重（默认路径）──────────────────────────────────────────
# 目录由本机已有的 HF 缓存整理而来，或执行 backend/scripts/prepare_prebuilt_models.py 生成。
FROM scratch AS models-local
COPY prebuilt-models /models

# ── 阶段 2b：构建期下载权重（MODELS_SOURCE=download 时使用）──────────────────
FROM python:3.12-slim AS models-download
ARG HF_ENDPOINT_BUILD=https://hf-mirror.com
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com
ENV HF_ENDPOINT=${HF_ENDPOINT_BUILD} \
    HF_HOME=/tmp/hf-cache \
    HF_HUB_DISABLE_XET=1 \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    HF_HUB_DOWNLOAD_TIMEOUT=120
# 用 Python API 而非 huggingface-cli：hub>=1.0 已废弃该命令（且不再提供 cli extra）。
# 必须白名单：仓库里的 imgs/.DS_Store 在镜像站返回 403，整仓下载必失败；
# 各仓库权重后缀不同，且 FlagEmbedding 还需要 colbert_linear.pt / sparse_linear.pt。
RUN --mount=type=cache,target=/tmp/hf-cache \
    python -m pip install --no-cache-dir -q huggingface_hub hf-transfer \
        -i ${PIP_INDEX_URL} --trusted-host ${PIP_TRUSTED_HOST} \
 && python -c "from huggingface_hub import snapshot_download; \
snapshot_download('BAAI/bge-m3', local_dir='/models/bge-m3', \
  allow_patterns=['*.json', '*.txt', '*.model', '*.pt', 'pytorch_model.bin']); \
snapshot_download('BAAI/bge-reranker-v2-m3', local_dir='/models/bge-reranker-v2-m3', \
  allow_patterns=['*.json', '*.txt', '*.model', '*.safetensors'])"

# ── 阶段 3：选择权重来源 ─────────────────────────────────────────────────────
FROM models-${MODELS_SOURCE} AS model-source

# ── 阶段 4：最终镜像 ─────────────────────────────────────────────────────────
FROM runtime-base
ENV MODELS_DIR=/app/models \
    HF_ENDPOINT=https://hf-mirror.com \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_XET=1 \
    EMBEDDING_MODEL_PATH=/app/models/bge-m3 \
    RERANK_MODEL_PATH=/app/models/bge-reranker-v2-m3
COPY --from=model-source /models /app/models
COPY src ./src

RUN mkdir -p uploads logs

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
