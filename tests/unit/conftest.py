import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

import mint_litellm

MIGRATION = Path(mint_litellm.__file__).parent / "control" / "db" / "migrations" / "001_initial.sql"
DATABASE_URL = os.environ.get("MINT_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def migrated_database() -> Iterator[str]:
    """일회용 데이터베이스를 만들어 초기 스키마를 적용하고 접속 주소를 돌려준다."""
    if not DATABASE_URL:
        pytest.skip("MINT_TEST_DATABASE_URL is not set")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"mint_unit_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(DATABASE_URL)
    params["dbname"] = name
    url = make_conninfo(**params)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
        yield url
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db(migrated_database):
    """테스트마다 롤백되는 트랜잭션. 실패를 기대하는 문장은 ``fails``로 감싼다."""
    import psycopg

    with psycopg.connect(migrated_database) as conn:
        yield conn
        conn.rollback()
