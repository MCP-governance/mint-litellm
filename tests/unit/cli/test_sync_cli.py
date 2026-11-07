"""``sync status``와 ``sync resume`` 명령."""

import json

import pytest

psycopg = pytest.importorskip("psycopg")

from .test_cli import Cli, bootstrap  # noqa: E402

HEX = "ab" * 32


@pytest.fixture
def mint(fresh_db, stub, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Cli(fresh_db, stub.url, capsys, tmp_path)


def seed(url: str, *, held: bool) -> str:
    with psycopg.connect(url) as conn:
        conn.execute(
            "INSERT INTO mint.servers (id, instance_id, server_id, status, url_sha256, transport,"
            " auth_type, registered_by) VALUES (gen_random_uuid(), 'clitest', 'srv-1',"
            " 'awaiting_approval', %s, 'http', 'none', 't')", (HEX,))
        row = conn.execute(
            "INSERT INTO mint.litellm_grants (id, instance_id, target_type, target_id, item,"
            " server_id, observed, missing_count, held, held_at, missing_window_start)"
            " VALUES (gen_random_uuid(), 'clitest', 'key', 'k1', 'mcp_server', 'srv-1', %s,"
            " %s, %s, CASE WHEN %s THEN now() END, now()) RETURNING id",
            (not held, 2 if held else 1, held, held)).fetchone()
        conn.commit()
        return str(row[0])


def test_status_lists_the_grants_and_filters_held_ones(mint):
    boss = bootstrap(mint)
    grant_id = seed(mint.env["MINT_DATABASE_URL"], held=True)
    code, out, err = mint.run("sync", "status", "--json", token=boss)
    assert code == 0, err
    [grant] = json.loads(out)["grants"]
    assert (grant["id"], grant["held"], grant["missing_count"]) == (grant_id, True, 2)
    code, out, _ = mint.run("sync", "status", "--held", token=boss)
    assert code == 0 and grant_id in out and "보류" in out


def test_resume_needs_admin_and_clears_the_hold(mint):
    boss = bootstrap(mint)
    viewer = mint.token(mint.run("operator", "add", "eye", "--role", "viewer", token=boss)[1])
    grant_id = seed(mint.env["MINT_DATABASE_URL"], held=True)
    assert mint.run("sync", "status", token=viewer)[0] == 0
    code, _, err = mint.run("sync", "resume", grant_id, token=viewer)
    assert code == 4 and "역할로 실행할 수 없습니다" in err
    code, _, err = mint.run("sync", "resume", grant_id, token=boss)
    assert code == 0, err
    with psycopg.connect(mint.env["MINT_DATABASE_URL"]) as conn:
        held, count = conn.execute(
            "SELECT held, missing_count FROM mint.litellm_grants WHERE id = %s", (grant_id,)
        ).fetchone()
        job = conn.execute("SELECT kind, target_ref FROM mint.jobs").fetchone()
        kinds = [r[0] for r in conn.execute("SELECT kind FROM mint.events")]
    assert (held, count) == (False, 0)
    assert job == ("sync_target", "key:k1") and "sync_applied" in kinds
    code, _, err = mint.run("sync", "resume", grant_id, token=boss)
    assert code == 1 and "보류" in err  # 이미 풀린 항목


def test_resume_of_an_unknown_grant_is_not_found(mint):
    boss = bootstrap(mint)
    code, _, err = mint.run("sync", "resume", "00000000-0000-4000-8000-000000000000",
                            token=boss)
    assert code == 1 and "없습니다" in err
