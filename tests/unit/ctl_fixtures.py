"""제어 평면 시험이 공유하는 고정 장치."""

import os
import uuid
from collections.abc import Iterator

import pytest
from litellm_stub import CONTROL_KEY, StubLiteLLM

import mint_litellm.control.db.migrate as migrate
from mint_litellm.control.context import Context, Operator
from mint_litellm.control.litellm_admin import LiteLLMAdmin

DATABASE_URL = os.environ.get("MINT_TEST_DATABASE_URL")
INSTANCE = "inst1"


def make_ctx(conn, name: str = "root", role: str = "admin", user_id: str | None = "root-user"):
    return Context(conn, INSTANCE, Operator(name, role, user_id))


@pytest.fixture
def stub() -> Iterator[StubLiteLLM]:
    server = StubLiteLLM()
    yield server
    server.close()


@pytest.fixture
def litellm(stub: StubLiteLLM) -> LiteLLMAdmin:
    return LiteLLMAdmin(stub.url, CONTROL_KEY)


@pytest.fixture
def fresh_db() -> Iterator[str]:
    """마이그레이션 실행기로 초기화한 일회용 데이터베이스."""
    if not DATABASE_URL:
        pytest.skip("MINT_TEST_DATABASE_URL is not set")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"mint_ctl_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(DATABASE_URL)
    params["dbname"] = name
    url = make_conninfo(**params)
    try:
        with psycopg.connect(url) as conn:
            migrate.apply_migrations(conn)
        yield url
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
