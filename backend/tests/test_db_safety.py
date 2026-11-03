"""测试库隔离守卫：pytest 默认不得写开发库（kp_dev）。

背景：测试会真实创建知识库。直连开发库会让测试知识库混入用户列表，
数量变多后把真实知识库挤出前端渲染窗口，表现为"新建的知识库消失"。
"""

import pytest

from src.infrastructure.db_safety import assert_test_database, is_dev_database

DEV_URL = "postgresql+asyncpg://u:p@localhost:5432/kp_dev"
TEST_URL = "postgresql+asyncpg://u:p@localhost:5432/kp_test"
CI_URL = "postgresql+asyncpg://u:p@localhost:5432/kp_ci"


def test_is_dev_database_detects_dev_names():
    assert is_dev_database(DEV_URL)
    assert is_dev_database("postgresql+asyncpg://u:p@db:5432/knowledge_platform_dev")
    assert not is_dev_database(TEST_URL)
    assert not is_dev_database(CI_URL)


def test_assert_test_database_rejects_dev_by_default(monkeypatch):
    monkeypatch.delenv("DB_ALLOW_DEV_TESTS", raising=False)
    with pytest.raises(RuntimeError, match="开发库"):
        assert_test_database(DEV_URL)


def test_assert_test_database_allows_isolated_db():
    assert_test_database(TEST_URL)
    assert_test_database(CI_URL)


def test_assert_test_database_explicit_override(monkeypatch):
    monkeypatch.setenv("DB_ALLOW_DEV_TESTS", "1")
    assert_test_database(DEV_URL)
