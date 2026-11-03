"""마이그레이션 실행기. MINT_TEST_DATABASE_URL이 없으면 건너뛴다."""

import pytest

psycopg = pytest.importorskip("psycopg")

from mint_litellm.control.db import migrate  # noqa: E402
from mint_litellm.control.errors import ControlError  # noqa: E402


def test_applies_once_and_is_a_no_op_the_second_time(fresh_db):
    with psycopg.connect(fresh_db) as conn:
        assert migrate.apply_migrations(conn) == []
        names = [r[0] for r in conn.execute("SELECT name FROM mint.schema_migrations")]
        assert names == ["001_initial"]


def test_first_run_on_an_empty_database_applies_everything(fresh_db):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(fresh_db)
    admin = dict(params, dbname="postgres")
    name = params["dbname"] + "_b"
    with psycopg.connect(make_conninfo(**admin), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(make_conninfo(**dict(params, dbname=name))) as conn:
            assert migrate.apply_migrations(conn) == ["001_initial"]
            assert conn.execute("SELECT count(*) FROM mint.orgs").fetchone()[0] == 0
    finally:
        with psycopg.connect(make_conninfo(**admin), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def test_a_changed_applied_migration_is_refused(fresh_db):
    with psycopg.connect(fresh_db) as conn:
        conn.execute("UPDATE mint.schema_migrations SET sha256 = repeat('0', 64)")
        with pytest.raises(ControlError, match="바뀌었습니다"):
            migrate.apply_migrations(conn)
