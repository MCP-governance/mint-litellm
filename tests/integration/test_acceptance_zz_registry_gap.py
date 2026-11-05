"""1단계 인수시험: MINT에 등록된 서버가 레지스트리에서 사라진 구성의 기동 점검.

다른 시험의 기동에 영향을 주므로 마지막에 실행하고 끝나면 서버를 같은 식별자로 되돌린다.
"""

import json
import time

import support
from scenario import db_server, decision, delete_db_server
from test_acceptance_core import code_of
from test_acceptance_review import run_start
from test_acceptance_surface import DB_SERVER_URL


def test_registered_server_missing_from_registry_stops_start_and_unverified_blocks(world, evidence):
    evidence.positive_control()
    server_id = db_server("mcp_gap", DB_SERVER_URL)
    subject = world.subject((server_id,))
    world.approve(subject, world.prepare(subject, server_id, url=DB_SERVER_URL), tools="echo")
    delete_db_server(server_id)
    since = world.now()
    try:
        # 다중 워커로 시작하면 워커마다 점검하고, 하나라도 실패하면 프로세스 전체가 끝난다.
        started = time.monotonic()
        done = run_start(
            "./config/litellm.mint.yaml",
            "--config",
            "/app/config/litellm.config.yaml",
            "--port",
            "4000",
            "--num_workers",
            "2",
        )
        output = done.stdout + done.stderr
        assert output.count("MINT 자가 점검 실패") == 2, output[-600:]
        assert "Stopping parent process" in output, output[-600:]
        print(
            f"multi-worker start stopped after {time.monotonic() - started:.0f}s "
            f"exit code {done.returncode}"
        )
        events = world.query(
            "select payload from mint.events where kind = 'enforcement_unverified' "
            "and inserted_at >= %s",
            (since,),
        )
        assert events
        assert all(e["payload"]["failed"][0]["name"] == "SERVER_REGISTRY_MATCH" for e in events)
        assert len({e["payload"]["worker_id"] for e in events}) == 2

        # 중단하지 않는 설정(MINT_REQUIRED=false)에서는 점검 실패 상태로 올라와 모든 호출을 막는다.
        support.compose(
            "up", "-d", "--wait", "--force-recreate", "litellm2", env={"MINT_E_REQUIRED": "false"}
        )
        mark = support.marker()
        other = world.subject(("a",))
        world.approve(other, world.prepare(other))
        blocked = world.call(other, port=support.LITELLM_PORTS[1], mark=mark)
        assert blocked.status == 403
        assert blocked.code == code_of("ENFORCEMENT_UNVERIFIED"), blocked.body
        evidence.assert_calls(mark, 0)
        assert decision(world.events(mark)[0])[:2] == ("block", "block")
    finally:
        db_server("mcp_gap", DB_SERVER_URL, server_id)
    # 서버를 되돌려 다시 시작하면 집행 점검이 통과하고 호출이 다시 허용된다.
    support.compose("up", "-d", "--wait", "--force-recreate", "litellm2")
    support.wait_until(
        lambda: world.call(other, port=support.LITELLM_PORTS[1]).allowed, 120, 3, what="recovery"
    )
    verified = world.query(
        "select count(*) as n from mint.events where kind = 'enforcement_verified' "
        "and inserted_at >= %s",
        (since,),
    )
    assert verified[0]["n"] >= 1
    assert json.loads(world.mint.run("status", check=False).stdout)["enforcement"]
