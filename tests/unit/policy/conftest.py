import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "src/mint_litellm/control/db/migrations/001_initial.sql"
REQUIRED_ENV = ("MINT_TEST_DATABASE_URL", "MINT_TEST_OPA_URL", "MINT_TEST_OPA_BUNDLE_DIR")


@pytest.fixture
def live_database() -> Iterator[str]:
    if not all(os.environ.get(key) for key in REQUIRED_ENV):
        pytest.skip("live database, OPA URL and bundle directory are required")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    admin_url = os.environ["MINT_TEST_DATABASE_URL"]
    name = f"mint_policy_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(admin_url)
    params["dbname"] = name
    url = make_conninfo(**params)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
