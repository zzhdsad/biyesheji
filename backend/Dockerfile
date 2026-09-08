FROM python:3.12-slim

WORKDIR /app

# 国内构建默认走阿里云 PyPI 镜像（--build-arg PIP_INDEX_URL= 可覆盖）
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

COPY requirements.txt .

# 第一步：核心运行时依赖（fastapi/sqlalchemy 等），PyTorch 走 CPU-only 索引减体积
RUN pip install --no-cache-dir -r requirements.txt \
    -i ${PIP_INDEX_URL} \
    --trusted-host ${PIP_TRUSTED_HOST} \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    --timeout 300 --retries 10

# 第二步：AI 重依赖（FlagEmbedding + sentence-transformers）
# FlagEmbedding 同时覆盖 Embedding（BGE-M3）和 Rerank（BGE-Reranker-v2-m3）
# 镜像体积会增 ~2GB，构建耗时较长，但模型权重（~2.3GB）仍在首次使用时从 HF 下载
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

COPY src ./src

RUN mkdir -p uploads logs

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
