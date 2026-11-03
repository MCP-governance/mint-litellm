"""프로세스별 로그인 역할. 그룹 역할은 마이그레이션이 만들고, 로그인 역할은 여기서 만든다."""

import psycopg
from psycopg import sql

from mint_litellm.control.db.connection import fetchone

LOGIN_ROLES = {"mint_plugin": "mint_plugin_app", "mint_control": "mint_control_app"}


def ensure_login_roles(conn: psycopg.Connection, passwords: dict[str, str]) -> list[str]:
    """``passwords``: 그룹 역할 이름 -> 로그인 비밀번호. 새로 만든 로그인 역할 이름을 돌려준다."""
    created: list[str] = []
    for group, password in passwords.items():
        login = sql.Identifier(LOGIN_ROLES[group])
        if fetchone(conn, "SELECT 1 AS x FROM pg_roles WHERE rolname = %s", (LOGIN_ROLES[group],)):
            conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                login, sql.Literal(password)))
            continue
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
            login, sql.Literal(password), sql.Identifier(group)))
        created.append(LOGIN_ROLES[group])
    return created
