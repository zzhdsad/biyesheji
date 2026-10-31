"""环境归属守卫测试：清理目标必须与已登记环境画像完全一致，否则拒绝。

覆盖：错误数据库、共享上传目录、正式 Milvus 集合、后端连接库不一致、
未挂载上传目录、主库未显式授权等组合。全部为纯逻辑校验，不触碰任何真实数据。
"""

from __future__ import annotations

import base64
import json

import pytest

from src.infrastructure.env_guard import (
    EnvironmentFacts,
    check_environment,
    facts_from_b64,
)


def _facts(**overrides) -> EnvironmentFacts:
    base = {
        "db": "kp_dev",
        "backend_db": "kp_dev",
        "milvus_collection": "document_chunks_dev",
        "upload_source": "/data/docker-data/uploads_dev",
        "upload_container_dir": "/app/uploads",
        "allow_main": False,
    }
    base.update(overrides)
    return EnvironmentFacts(**base)


class TestDevEnvironment:
    def test_matching_dev_target_is_allowed(self):
        result = check_environment(_facts())
        assert result.allowed is True
        assert result.profile == "kp_dev"
        assert result.environment["milvus_collection"] == "document_chunks_dev"

    def test_windows_style_upload_path_is_allowed(self):
        result = check_environment(_facts(upload_source=r"D:\docker-data\uploads_dev"))
        assert result.allowed is True

    def test_trailing_separator_is_tolerated(self):
        result = check_environment(_facts(upload_source="D:/docker-data/uploads_dev/"))
        assert result.allowed is True

    def test_unknown_database_is_rejected(self):
        result = check_environment(_facts(db="some_other_db", backend_db="some_other_db"))
        assert result.allowed is False
        assert "未登记" in result.reason

    def test_empty_database_is_rejected(self):
        result = check_environment(_facts(db="", backend_db=""))
        assert result.allowed is False

    def test_shared_production_upload_dir_is_rejected(self):
        result = check_environment(_facts(upload_source="/data/docker-data/uploads"))
        assert result.allowed is False
        assert "上传目录" in result.reason

    def test_unmounted_upload_dir_is_rejected(self):
        result = check_environment(_facts(upload_source=""))
        assert result.allowed is False
        assert "未检测到上传目录挂载" in result.reason

    def test_wrong_container_mount_point_is_rejected(self):
        result = check_environment(_facts(upload_container_dir="/app/uploads_legacy"))
        assert result.allowed is False

    def test_production_milvus_collection_is_rejected(self):
        result = check_environment(_facts(milvus_collection="document_chunks"))
        assert result.allowed is False
        assert "Milvus 集合" in result.reason

    def test_missing_milvus_collection_is_rejected(self):
        result = check_environment(_facts(milvus_collection=""))
        assert result.allowed is False
        assert "Milvus" in result.reason

    def test_backend_connected_to_other_db_is_rejected(self):
        result = check_environment(_facts(backend_db="knowledge_platform"))
        assert result.allowed is False
        assert "不一致" in result.reason

    def test_unknown_backend_db_is_rejected(self):
        result = check_environment(_facts(backend_db=""))
        assert result.allowed is False

    def test_mixed_environment_config_is_rejected(self):
        # 开发库 + 正式集合 + 正式上传目录：任一错配都必须拒绝
        result = check_environment(
            _facts(
                milvus_collection="document_chunks",
                upload_source="/data/docker-data/uploads",
            )
        )
        assert result.allowed is False


class TestMainEnvironment:
    def test_main_requires_allow_main(self):
        result = check_environment(
            _facts(
                db="knowledge_platform",
                backend_db="knowledge_platform",
                milvus_collection="document_chunks",
                upload_source="/data/docker-data/uploads",
                allow_main=False,
            )
        )
        assert result.allowed is False
        assert "AllowMain" in result.reason

    def test_main_with_allow_and_matching_targets_is_allowed(self):
        result = check_environment(
            _facts(
                db="knowledge_platform",
                backend_db="knowledge_platform",
                milvus_collection="document_chunks",
                upload_source="/data/docker-data/uploads",
                allow_main=True,
            )
        )
        assert result.allowed is True

    def test_main_with_dev_upload_dir_is_rejected(self):
        result = check_environment(
            _facts(
                db="knowledge_platform",
                backend_db="knowledge_platform",
                milvus_collection="document_chunks",
                upload_source="/data/docker-data/uploads_dev",
                allow_main=True,
            )
        )
        assert result.allowed is False

    def test_main_with_dev_milvus_collection_is_rejected(self):
        result = check_environment(
            _facts(
                db="knowledge_platform",
                backend_db="knowledge_platform",
                milvus_collection="document_chunks_dev",
                upload_source="/data/docker-data/uploads",
                allow_main=True,
            )
        )
        assert result.allowed is False


class TestFactsDecoding:
    def test_facts_from_base64_roundtrip(self):
        payload = {
            "db": "kp_dev",
            "backend_db": "kp_dev",
            "milvus_collection": "document_chunks_dev",
            "upload_source": "/data/uploads_dev",
            "upload_container_dir": "/app/uploads",
            "allow_main": False,
        }
        raw = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
        facts = facts_from_b64(raw)
        assert check_environment(facts).allowed is True

    def test_invalid_payload_is_rejected(self):
        raw = base64.b64encode(b'"not-an-object"').decode("ascii")
        with pytest.raises(ValueError):
            facts_from_b64(raw)
