# 模型权重镜像：把 prebuilt-models 单独打包，避免每次后端构建都发送 4.3GB 上下文。
# 构建一次即可（仅当模型更新时重建）：
#   docker build -t biyesheji-models:latest -f Dockerfile.models backend/models-image
FROM scratch
COPY prebuilt-models /models
