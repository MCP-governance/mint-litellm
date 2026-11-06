"""프로세스별 로그인 역할(작업 실행기, 직원 풀)과 그 권한. MINT_TEST_DATABASE_URL이 없으면 건너뛴다."""

import pytest
from ctl_fixtures import INSTANCE

psycopg = pytest.importorskip("psycopg")

from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

from mint_litellm.control.db.roles import (  # noqa: E402
    LOGIN_MEMBERSHIPS,
    LOGIN_PASSWORD_VARIABLES,
    LOGIN_ROLES,
    ensure_login_roles,
)

PASSWORDS = {key: f"pw-{key}" for key in LOGIN_ROLES}


def as_login(url: str, key: str):
    params = conninfo_to_dict(url)
    params.update(user=LOGIN_ROLES[key], password=PASSWORDS[key])
    return psycopg.connect(make_conninfo(**params))


@pytest.fixture
def roles(fresh_db):
    with psycopg.connect(fresh_db) as conn:
        configured = ensure_login_roles(conn, PASSWORDS)
    yield configured
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        for login in LOGIN_ROLES.values():
            conn.execute(f'DROP OWNED BY "{login}"')
            conn.execute(f'DROP ROLE IF EXISTS "{login}"')


def members(url: str, login: str) -> set[str]:
    with psycopg.connect(url) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT g.rolname FROM pg_auth_members a JOIN pg_roles g ON g.oid = a.roleid"
                " JOIN pg_roles m ON m.oid = a.member WHERE m.rolname = %s",
                (login,),
            )
        }


def test_the_login_role_table_covers_every_process():
    assert set(LOGIN_ROLES) == set(LOGIN_PASSWORD_VARIABLES)
    assert LOGIN_MEMBERSHIPS["mint_worker"] == ("mint_control", "mint_worker", "mint_sealer")
    assert set(LOGIN_ROLES) == {"mint_plugin", "mint_control", "mint_worker", "mint_staff"}


def test_login_roles_receive_the_groups_of_their_process(fresh_db, roles):
    assert sorted(roles) == sorted(LOGIN_ROLES.values())
    assert members(fresh_db, "mint_worker_app") == {"mint_control", "mint_worker", "mint_sealer"}
    assert members(fresh_db, "mint_staff_app") == {"mint_staff"}
    assert members(fresh_db, "mint_control_app") == {"mint_control"}
    assert members(fresh_db, "mint_plugin_app") == {"mint_plugin"}


def test_running_it_again_only_resets_the_passwords(fresh_db, roles):
    with psycopg.connect(fresh_db) as conn:
        assert ensure_login_roles(conn, {"mint_worker": "another"}) == ["mint_worker_app"]
    assert members(fresh_db, "mint_worker_app") == {"mint_control", "mint_worker", "mint_sealer"}


def test_the_worker_login_reaches_jobs_and_the_staff_login_only_the_functions(fresh_db, roles):
    with as_login(fresh_db, "mint_worker") as conn:
        assert conn.execute("SELECT count(*) FROM mint.jobs").fetchone() == (0,)
        conn.execute(
            "INSERT INTO mint.jobs (id, instance_id, kind, target_ref)"
            " VALUES (gen_random_uuid(), %s, 'seal', 'x')",
            (INSTANCE,),
        )
        conn.execute(
            "UPDATE mint.jobs SET status = 'running', lease_owner = 'w',"
            " lease_until = now() + interval '1 minute'"
        )
        conn.rollback()
    with as_login(fresh_db, "mint_staff") as conn:
        conn.execute(
            "SELECT set_config('mint.staff_user_id', 'human-1', true),"
            " set_config('mint.staff_instance_id', %s, true)",
            (INSTANCE,),
        )
        assert conn.execute("SELECT count(*) FROM mint.staff_list_consents()").fetchone() == (0,)
        conn.rollback()
        for query in ("SELECT * FROM mint.consents", "SELECT * FROM mint.events",
                      "SELECT * FROM mint.jobs", "SELECT * FROM mint.settings"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(query)
            conn.rollback()
