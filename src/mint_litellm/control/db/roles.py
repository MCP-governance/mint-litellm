"""프로세스별 로그인 역할. 그룹 역할은 마이그레이션이 만들고, 로그인 역할은 여기서 만든다.

로그인 역할 이름 -> 받는 그룹 역할은 프로세스의 권한 구성이다. 작업 실행기는 제어·작업·봉인
그룹을, 콘솔의 직원 화면 연결 풀은 직원 그룹만 받는다.
"""

import psycopg
from psycopg import sql

from mint_litellm.control.db.connection import fetchone

# 키: 로그인 역할을 가리키는 이름(비밀번호를 주는 단위), 값: 로그인 역할 이름
LOGIN_ROLES = {
    "mint_plugin": "mint_plugin_app",
    "mint_control": "mint_control_app",
    "mint_worker": "mint_worker_app",
    "mint_staff": "mint_staff_app",
}

# 로그인 역할이 받는 그룹 역할. 적지 않은 키는 자기 이름의 그룹 하나만 받는다.
LOGIN_MEMBERSHIPS = {"mint_worker": ("mint_control", "mint_worker", "mint_sealer")}

# 로그인 역할 비밀번호를 담는 환경 변수
LOGIN_PASSWORD_VARIABLES = {
    "mint_plugin": "MINT_PLUGIN_DB_PASSWORD",
    "mint_control": "MINT_CONTROL_DB_PASSWORD",
    "mint_worker": "MINT_WORKER_DB_PASSWORD",
    "mint_staff": "MINT_STAFF_DB_PASSWORD",
}


def ensure_login_roles(conn: psycopg.Connection, passwords: dict[str, str]) -> list[str]:
    """``passwords``: LOGIN_ROLES의 키 -> 로그인 비밀번호. 설정한 로그인 역할 이름을 돌려준다."""
    configured: list[str] = []
    for key, password in passwords.items():
        login = sql.Identifier(LOGIN_ROLES[key])
        groups = LOGIN_MEMBERSHIPS.get(key, (key,))
        if fetchone(conn, "SELECT 1 AS x FROM pg_roles WHERE rolname = %s", (LOGIN_ROLES[key],)):
            conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                login, sql.Literal(password)))
            # 다시 실행해도 그룹 구성을 설계 표에 맞춘다.
            conn.execute(sql.SQL("GRANT {} TO {}").format(
                sql.SQL(", ").join(sql.Identifier(g) for g in groups), login))
        else:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                login, sql.Literal(password),
                sql.SQL(", ").join(sql.Identifier(g) for g in groups)))
        configured.append(LOGIN_ROLES[key])
    return configured
