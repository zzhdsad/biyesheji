"""预下载 BGE-M3 与 bge-reranker-v2-m3 到本地目录（走 hf-mirror，跳过 imgs/.DS_Store 等垃圾文件）。

下到 ./models/ 下后，把 .env 的 EMBEDDING_MODEL / RERANK_MODEL 指向本地目录，
FlagEmbedding 检测到路径存在即直接加载，不再触发 snapshot_download（规避镜像 403）。

用法：python download_models.py
"""
import os

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")

from huggingface_hub import snapshot_download

# 跳过 macOS 垃圾、非推理用格式（flax/rust/tf/onnx），只留 PyTorch 推理所需
IGNORE = [
    "imgs/*", "*.DS_Store",
    "flax_model.msgpack", "rust_model.ot", "tf_model.h5",
    "*.onnx", "onnx/*",
]

ROOT = os.path.dirname(os.path.abspath(__file__))


def fetch(repo_id: str, local_dir: str) -> str:
    print(f"开始下载 {repo_id} -> {local_dir} ...", flush=True)
    path = snapshot_download(
        repo_id=repo_id,
        local_dir=local_dir,
        local_dir_use_symlinks=False,  # Windows 无符号链接权限，直接拷贝
        ignore_patterns=IGNORE,
    )
    size = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _, fs in os.walk(path)
        for f in fs
    ) / 1024 / 1024
    print(f"完成 {repo_id}: {path} ({size:.0f} MB)", flush=True)
    return path


if __name__ == "__main__":
    os.makedirs(os.path.join(ROOT, "models"), exist_ok=True)
    fetch("BAAI/bge-m3", os.path.join(ROOT, "models", "bge-m3"))
    fetch("BAAI/bge-reranker-v2-m3", os.path.join(ROOT, "models", "bge-reranker-v2-m3"))
    print("全部下载完成 ✅", flush=True)
