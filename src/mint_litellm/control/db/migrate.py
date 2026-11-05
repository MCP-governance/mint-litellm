"""마이그레이션 실행기. ``migrations/NNN_*.sql``을 번호 순으로 한 번씩 적용한다.

적용한 파일의 해시를 기록해, 이미 적용한 파일이 바뀌었으면 실행을 거부한다.
"""

import hashlib
from importlib import resources

import psycopg

from mint_litellm.control.db.connection import fetchall
from mint_litellm.control.errors import ControlError

_LOCK_KEY = 0x4D494E54  # 동시에 실행된 실행기가 같은 파일을 두 번 적용하지 않게 한다.


def _migration_files() -> list[tuple[str, str]]:
    root = resources.files("mint_litellm.control.db") / "migrations"
    files = sorted((f for f in root.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name)
    return [(f.name.removesuffix(".sql"), f.read_text(encoding="utf-8")) for f in files]


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """아직 적용하지 않은 마이그레이션을 적용하고 이번에 적용한 이름을 돌려준다."""
    applied_now: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        conn.execute("CREATE SCHEMA IF NOT EXISTS mint")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS mint.schema_migrations ("
            " name text PRIMARY KEY, sha256 text NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        rows = fetchall(conn, "SELECT name, sha256 FROM mint.schema_migrations")
        done = {r["name"]: r["sha256"] for r in rows}
        for name, sql in _migration_files():
            digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            if name in done:
                if done[name] != digest:
                    raise ControlError(f"적용된 마이그레이션 {name}의 내용이 바뀌었습니다.")
                continue
            conn.execute(sql.encode("utf-8"))
            conn.execute(
                "INSERT INTO mint.schema_migrations (name, sha256) VALUES (%s, %s)", (name, digest)
            )
            applied_now.append(name)
    return applied_now
