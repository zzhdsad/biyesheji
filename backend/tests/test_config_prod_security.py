"""生产环境配置校验单元测试（不连接数据库、不启动容器）。

覆盖：
- ENV=prod / production（及大小写、首尾空白变体）时拒绝示例密钥、默认数据库/
  MinIO 凭据、默认管理员密码和缺失配置，并接受完整的安全配置；
- 未知 ENV 值直接报错，不能绕过生产安全校验；
- ENV 归一化（production → prod）与 dev / test / testing 的兼容行为；
- 五类凭据统一拒绝 CHANGE_ME / YOUR_PASSWORD / REPLACE_THIS 等占位符；
- Compose 直接拼接 DATABASE_URL 的 URL 特殊字符边界（@、%XX、可往返字符）；
- DATABASE_URL 密码只做一次 URL 解码（百分号、URL 编码字符等边界）；
- 配置错误信息不回显真实凭据或完整连接字符串。

运行方式（跳过依赖数据库/服务的 conftest）：
    python -m pytest --noconftest tests/test_config_prod_security.py -q
"""

import pytest
from pydantic import ValidationError

from src.core.config import (
    DEV_DEFAULT_ADMIN_PASSWORD,
    DEV_DEFAULT_MINIO_ACCESS_KEY,
    DEV_DEFAULT_MINIO_SECRET_KEY,
    DEV_DEFAULT_SECRET_KEY,
    Settings,
    _database_password,
)

# 仅用于测试的假凭据，不对应任何真实环境
SECURE_PROD = {
    "ENV": "prod",
    "SECRET_KEY": "test-only-secret-key-0123456789abcdef0123456789abcdef",
    "DATABASE_URL": (
        "postgresql+asyncpg://kp_app:ProdDbPassw0rd2026@db:5432/knowledge_platform"
    ),
    "MINIO_ACCESS_KEY": "kp-prod-access-2026",
    "MINIO_SECRET_KEY": "ProdMinioSecret2026",
    "DEFAULT_ADMIN_EMAIL": "ops@example.com",
    "DEFAULT_ADMIN_USERNAME": "opsadmin",
    "DEFAULT_ADMIN_PASSWORD": "ProdAdminPassw0rd2026",
}


def _make_settings(**overrides) -> Settings:
    """构造独立于本机 .env 的 Settings（显式传入所有被校验字段）。"""
    values = {**SECURE_PROD, **overrides}
    return Settings(_env_file=None, **values)


def _error(**overrides) -> str:
    with pytest.raises(ValidationError) as exc:
        _make_settings(**overrides)
    return str(exc.value)


def test_prod_accepts_complete_secure_config():
    settings = _make_settings()
    assert settings.ENV == "prod"
    assert settings.DEFAULT_ADMIN_PASSWORD == SECURE_PROD["DEFAULT_ADMIN_PASSWORD"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("SECRET_KEY", DEV_DEFAULT_SECRET_KEY),
        ("SECRET_KEY", DEV_DEFAULT_SECRET_KEY.upper()),
        ("SECRET_KEY", "change-me-in-production"),
        ("SECRET_KEY", "short"),
        ("SECRET_KEY", ""),
        (
            "DATABASE_URL",
            "postgresql+asyncpg://postgres:postgres@db:5432/knowledge_platform",
        ),
        ("DATABASE_URL", "postgresql+asyncpg://kp_app@db:5432/knowledge_platform"),
        (
            "DATABASE_URL",
            "postgresql+asyncpg://kp_app:password@db:5432/knowledge_platform",
        ),
        (
            "DATABASE_URL",
            "postgresql+asyncpg://kp_app:short@db:5432/knowledge_platform",
        ),
        ("DATABASE_URL", "not-a-valid-url"),
        ("MINIO_ACCESS_KEY", DEV_DEFAULT_MINIO_ACCESS_KEY),
        ("MINIO_ACCESS_KEY", ""),
        ("MINIO_SECRET_KEY", DEV_DEFAULT_MINIO_SECRET_KEY),
        ("MINIO_SECRET_KEY", "short"),
        ("MINIO_SECRET_KEY", ""),
        ("DEFAULT_ADMIN_PASSWORD", DEV_DEFAULT_ADMIN_PASSWORD),
        ("DEFAULT_ADMIN_PASSWORD", "short"),
        ("DEFAULT_ADMIN_PASSWORD", ""),
        ("DEFAULT_ADMIN_EMAIL", ""),
        ("DEFAULT_ADMIN_EMAIL", "not-an-email"),
        ("DEFAULT_ADMIN_USERNAME", ""),
        ("DEFAULT_ADMIN_USERNAME", "ops admin"),
    ],
)
def test_prod_rejects_unsafe_or_missing_config(field, value):
    error = _error(**{field: value})
    assert field in error or (field == "DATABASE_URL" and "PostgreSQL" in error)


@pytest.mark.parametrize(
    "env",
    [
        "prod",
        "PROD",
        "Prod",
        " prod ",
        "production",
        "PRODUCTION",
        "Production",
        " production ",
    ],
)
def test_prod_aliases_trigger_security_checks(env):
    error = _error(ENV=env, SECRET_KEY=DEV_DEFAULT_SECRET_KEY)
    assert "SECRET_KEY" in error


@pytest.mark.parametrize(
    "env",
    ["prod", "PROD", " production ", "Production"],
)
def test_production_aliases_normalize_to_prod(env):
    assert _make_settings(ENV=env).ENV == "prod"


@pytest.mark.parametrize(
    "env,expected",
    [("DEV", "dev"), ("Test", "test"), (" TESTING ", "testing")],
)
def test_dev_and_test_aliases_normalize(env, expected):
    settings = Settings(_env_file=None, ENV=env)
    assert settings.ENV == expected


@pytest.mark.parametrize(
    "env",
    ["staging", "development", "local", "ci", "qa", "prodution", "", "   "],
)
def test_unknown_env_is_rejected(env):
    error = _error(ENV=env)
    assert "ENV" in error
    assert "dev / test / testing / prod / production" in error


def test_unknown_env_cannot_bypass_production_checks():
    error = _error(ENV="prodution", SECRET_KEY=DEV_DEFAULT_SECRET_KEY)
    assert "ENV" in error


@pytest.mark.parametrize("env", ["dev", "test", "testing"])
def test_dev_and_test_keep_existing_defaults(env):
    settings = Settings(
        _env_file=None,
        ENV=env,
        SECRET_KEY=DEV_DEFAULT_SECRET_KEY,
        DATABASE_URL=(
            "postgresql+asyncpg://postgres:postgres@localhost:5432/knowledge_platform"
        ),
        MINIO_ACCESS_KEY=DEV_DEFAULT_MINIO_ACCESS_KEY,
        MINIO_SECRET_KEY=DEV_DEFAULT_MINIO_SECRET_KEY,
        DEFAULT_ADMIN_PASSWORD=DEV_DEFAULT_ADMIN_PASSWORD,
    )
    assert settings.ENV == env
    assert settings.SECRET_KEY == DEV_DEFAULT_SECRET_KEY
    assert settings.DEFAULT_ADMIN_PASSWORD == DEV_DEFAULT_ADMIN_PASSWORD


def test_validation_error_does_not_echo_credentials():
    markers = {
        "SECRET_KEY": "TEST_ONLY_SECRET_MARKER_0123456789abcdef",
        "DATABASE_URL": (
            "postgresql+asyncpg://kp_app:TEST_ONLY_DB_MARKER@db:5432/knowledge_platform"
        ),
        "MINIO_ACCESS_KEY": "TEST_ONLY_ACCESS_MARKER",
        "MINIO_SECRET_KEY": "TEST_ONLY_MINIO_MARKER",
        # 只让管理员密码触发失败，其余字段保持安全值
        "DEFAULT_ADMIN_PASSWORD": "short",
    }
    error = _error(**markers)

    assert "DEFAULT_ADMIN_PASSWORD" in error
    for marker in (
        "TEST_ONLY_SECRET_MARKER",
        "TEST_ONLY_DB_MARKER",
        "TEST_ONLY_ACCESS_MARKER",
        "TEST_ONLY_MINIO_MARKER",
    ):
        assert marker not in error


@pytest.mark.parametrize(
    "encoded,expected",
    [
        # make_url() 已解码一次：@ / / 等字符直接还原
        ("Prod%40Passw0rd%2F2026", "Prod@Passw0rd/2026"),
        # 字面量 %2F（URL 中写作 %252F）必须保持为 %2F，不能被二次解码成 /
        ("Prod%25Pass%252F2026", "Prod%Pass%2F2026"),
        ("Prod%20Pass%23w0rd", "Prod Pass#w0rd"),
        # "+" 在 userinfo 中不是空格
        ("Prod+Passw0rd+2026", "Prod+Passw0rd+2026"),
    ],
)
def test_database_password_is_decoded_exactly_once(encoded, expected):
    url = f"postgresql+asyncpg://kp_app:{encoded}@db:5432/knowledge_platform"
    assert _database_password(url) == expected


def test_prod_accepts_password_with_literal_percent_sequence():
    # 字面量密码 "%61dmin123456"（URL 编码 %2561dmin123456）。
    # 若多做一次 unquote，会变成弱口令 "admin123456" 并被误判拒绝。
    url = "postgresql+asyncpg://kp_app:%2561dmin123456@db:5432/knowledge_platform"
    settings = _make_settings(
        DATABASE_URL=url, POSTGRES_PASSWORD="%61dmin123456"
    )
    assert settings.DATABASE_URL == url


def test_prod_rejects_url_encoded_weak_database_password():
    # p%6Fstgres 会被 make_url 解码为 "postgres"；Compose 不做编码，故按不兼容拒绝
    url = "postgresql+asyncpg://kp_app:p%6Fstgres@db:5432/knowledge_platform"
    error = _error(DATABASE_URL=url)
    assert "DATABASE_URL" in error


def test_database_url_and_password_never_echoed():
    url = "postgresql+asyncpg://kp_app:postgres@db:5432/knowledge_platform"
    error = _error(DATABASE_URL=url)

    assert "PostgreSQL" in error
    for marker in ("kp_app", "postgres@", "db:5432", "knowledge_platform", url):
        assert marker not in error


# ── 占位符拒绝 ──────────────────────────────────────────────────────────────
PLACEHOLDER_CREDENTIALS = [
    "CHANGE_ME",
    "change_me_access",
    "  Your_Password  ",
    "REPLACE_THIS",
    "your-secret",
    "placeholder-value",
    "CHANGE-ME-2026",
]


@pytest.mark.parametrize("value", PLACEHOLDER_CREDENTIALS)
@pytest.mark.parametrize(
    "field",
    ["SECRET_KEY", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "DEFAULT_ADMIN_PASSWORD"],
)
def test_prod_rejects_placeholder_credentials(field, value):
    error = _error(**{field: value})
    assert field in error


@pytest.mark.parametrize(
    "value",
    ["CHANGE_ME", "Your_Password", "REPLACE_THIS", "CHANGE-ME-2026"],
)
def test_prod_rejects_placeholder_database_password(value):
    url = f"postgresql+asyncpg://kp_app:{value}@db:5432/knowledge_platform"
    error = _error(DATABASE_URL=url, POSTGRES_PASSWORD=value)
    assert "PostgreSQL" in error


def test_placeholder_rejection_does_not_echo_value():
    value = "CHANGE_ME_ACCESS_MARKER_2026"
    error = _error(MINIO_ACCESS_KEY=value)

    assert "MINIO_ACCESS_KEY" in error
    assert value not in error


# ── Compose 拼接 DATABASE_URL 的 URL 特殊字符边界 ───────────────────────────


def _db_url(password: str) -> str:
    return f"postgresql+asyncpg://kp_app:{password}@db:5432/knowledge_platform"


def test_prod_rejects_password_with_at_sign():
    raw = "Prod@Passw0rd2026"
    error = _error(DATABASE_URL=_db_url(raw), POSTGRES_PASSWORD=raw)

    assert "DATABASE_URL" in error
    for marker in (raw, "kp_app", "db:5432"):
        assert marker not in error


def test_prod_rejects_percent_encoded_password_mismatch():
    raw = "Prod%40Passw0rd2026"
    error = _error(DATABASE_URL=_db_url(raw), POSTGRES_PASSWORD=raw)

    assert "DATABASE_URL" in error
    assert raw not in error


def test_prod_rejects_percent_escape_without_raw_password():
    raw = "Prod%40Passw0rd2026"
    error = _error(DATABASE_URL=_db_url(raw))

    assert "DATABASE_URL" in error
    assert "百分号" in error


def test_prod_accepts_literal_percent_password_that_roundtrips():
    # "%Pa" 不是合法百分号转义，make_url 原样保留，与真实密码一致
    raw = "Prod%Passw0rd2026"
    settings = _make_settings(DATABASE_URL=_db_url(raw), POSTGRES_PASSWORD=raw)

    assert settings.DATABASE_URL == _db_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "Prod:Passw0rd2026",
        "Prod/Passw0rd2026",
        "Prod?Passw0rd2026",
        "Prod#Passw0rd2026",
    ],
)
def test_prod_accepts_reserved_chars_that_roundtrip(raw):
    # 这些字符会被 make_url 原样保留并以明文传给 asyncpg，不会造成认证不一致
    settings = _make_settings(DATABASE_URL=_db_url(raw), POSTGRES_PASSWORD=raw)

    assert settings.DATABASE_URL == _db_url(raw)


def test_prod_rejects_user_mismatch():
    error = _error(POSTGRES_USER="other_user")

    assert "DATABASE_URL" in error
    assert "other_user" not in error


def test_prod_rejects_database_name_mismatch():
    error = _error(POSTGRES_DB="other_db")

    assert "DATABASE_URL" in error
    assert "other_db" not in error


def test_prod_rejects_corrupted_host_without_raw_password():
    raw = "Prod@Passw0rd2026"
    error = _error(DATABASE_URL=_db_url(raw))

    assert "DATABASE_URL" in error
    assert raw not in error


def test_url_error_does_not_echo_connection_string():
    raw = "TEST_ONLY_DB_MARKER@2026"
    url = _db_url(raw)
    error = _error(DATABASE_URL=url, POSTGRES_PASSWORD=raw)

    assert "DATABASE_URL" in error
    for marker in (raw, "TEST_ONLY_DB_MARKER", "kp_app", "db:5432", url):
        assert marker not in error


@pytest.mark.parametrize("env", ["dev", "test", "testing"])
@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://kp_app:Prod%40Passw0rd2026@db:5432/knowledge_platform",
        "postgresql+asyncpg://kp_app:Prod@Passw0rd2026@db:5432/knowledge_platform",
        "postgresql+asyncpg://kp_app:Prod?Passw0rd2026@db:5432/knowledge_platform",
    ],
)
def test_dev_and_test_keep_any_database_url(env, url):
    settings = Settings(_env_file=None, ENV=env, DATABASE_URL=url)
    assert settings.DATABASE_URL == url
