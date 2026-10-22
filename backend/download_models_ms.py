"""经 ModelScope（阿里魔搭，国内可达）下载 BGE-M3 与 bge-reranker-v2-m3 到本地目录。

HF 镜像对大文件走 xet（cas-server.xethub.hf.co 401/403），改走 ModelScope 规避。
下到 ./models/ 后，把 .env 的 EMBEDDING_MODEL / RERANK_MODEL 指向返回的本地路径，
FlagEmbedding 检测到路径存在即直接加载，不再触发任何下载。

用法：python download_models_ms.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(ROOT, "models")
os.makedirs(MODELS_DIR, exist_ok=True)

from modelscope import snapshot_download

# 只取 PyTorch 推理所需文件，跳过 onnx/imgs 等无关产物
ALLOW_PATTERNS = [
    "config.json", "config_sentence_transformers.json", "sentence_bert_config.json",
    "modules.json", "1_Pooling/*", "special_tokens_map.json", "tokenizer_config.json",
    "tokenizer.json", "sentencepiece.bpe.model", "vocab.txt",
    "pytorch_model.bin", "model.safetensors",
    "colbert_linear.pt", "sparse_linear.pt",
]


def fetch(model_id: str) -> str:
    print(f"开始下载 {model_id} ...", flush=True)
    path = snapshot_download(
        model_id=model_id,
        cache_dir=MODELS_DIR,
        allow_patterns=ALLOW_PATTERNS,
    )
    size = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _, fs in os.walk(path)
        for f in fs
    ) / 1024 / 1024
    print(f"完成 {model_id}: {path} ({size:.0f} MB)", flush=True)
    return path


if __name__ == "__main__":
    emb_path = fetch("BAAI/bge-m3")
    rer_path = fetch("BAAI/bge-reranker-v2-m3")
    print("\n===== 配置指引 =====", flush=True)
    print(f"EMBEDDING_MODEL={emb_path}", flush=True)
    print(f"RERANK_MODEL={rer_path}", flush=True)
    print("将上面两行写入 backend/.env（覆盖默认的 BAAI/... 仓库名）", flush=True)
    print("全部下载完成 ✅", flush=True)
