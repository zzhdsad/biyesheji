"""文件存储抽象：本地磁盘 / MinIO 可切换（依赖倒置，便于替换实现）。"""

import io
from abc import ABC, abstractmethod
from pathlib import Path

from loguru import logger

from src.core.config import settings


class StorageError(Exception):
    """存储层异常。"""


class BaseStorage(ABC):
    """存储接口：file_key 为文件唯一标识，如 '{kb_id}/{doc_id}.pdf'。"""

    @abstractmethod
    def save(self, file_key: str, content: bytes) -> str:
        """保存文件，返回存储引用（本地为相对路径，MinIO 为 s3:// URI）。"""

    @abstractmethod
    def delete(self, file_key: str) -> None:
        """删除文件（不存在时静默成功）。"""

    @abstractmethod
    def load(self, file_key: str) -> bytes:
        """读取文件内容（供后续解析任务使用）。"""


class LocalStorage(BaseStorage):
    """本地磁盘存储：默认 backend/{UPLOAD_DIR}/{file_key}。"""

    def _resolve(self, file_key: str) -> Path:
        base = Path(settings.UPLOAD_DIR).resolve()
        path = (base / file_key).resolve()
        # 防路径穿越：file_key 必须位于上传根目录内
        if not path.is_relative_to(base):
            raise StorageError(f"非法文件路径：{file_key}")
        return path

    def save(self, file_key: str, content: bytes) -> str:
        path = self._resolve(file_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return str(path)

    def delete(self, file_key: str) -> None:
        path = self._resolve(file_key)
        if path.exists():
            path.unlink()

    def load(self, file_key: str) -> bytes:
        path = self._resolve(file_key)
        if not path.exists():
            raise StorageError(f"文件不存在：{file_key}")
        return path.read_bytes()


class MinIOStorage(BaseStorage):
    """MinIO / S3 兼容对象存储。"""

    def __init__(self) -> None:
        from minio import Minio

        self._client = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=False,
        )
        self._bucket = settings.MINIO_BUCKET

    def _ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)
            logger.info(f"已创建 MinIO bucket：{self._bucket}")

    def save(self, file_key: str, content: bytes) -> str:
        self._ensure_bucket()
        self._client.put_object(
            self._bucket, file_key, io.BytesIO(content), length=len(content)
        )
        return f"s3://{self._bucket}/{file_key}"

    def delete(self, file_key: str) -> None:
        from minio.error import S3Error

        try:
            self._client.remove_object(self._bucket, file_key)
        except S3Error as exc:
            raise StorageError(f"MinIO 删除失败：{exc}") from exc

    def load(self, file_key: str) -> bytes:
        response = self._client.get_object(self._bucket, file_key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()


def get_storage() -> BaseStorage:
    """按配置返回存储实现。"""
    backend = settings.STORAGE_BACKEND.lower()
    if backend == "minio":
        return MinIOStorage()
    return LocalStorage()
