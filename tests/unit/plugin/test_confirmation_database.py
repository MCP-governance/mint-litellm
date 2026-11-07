"""본인 확인 실제 DB 시험: 두 복제본·상한·별도 표·소비 트랜잭션."""

import asyncio
import io
from dataclasses import replace
from uuid import uuid4

import ctl_fixtures
import pytest
from ctl_fixtures import LOGIN_PASSWORDS, as_login
from plugin_helpers import config, evaluator, request, user_principal
from test_plugin_database import Scenario, run

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

from mint_litellm.control.db.roles import LOGIN_ROLES  # noqa: E402
from mint_litellm.control.services import staff  # noqa: E402
from mint_litellm.plugin.buffer import (  # noqa: E402
    ConsumptionLost,
    EvidenceRecorder,
    LocalBuffer,
)
from mint_litellm.plugin.pipeline import (  # noqa: E402
    PipelineDeps,
    evaluate_request,
    match_call_approval,
    match_confirmation,
)
from mint_litellm.plugin.snapshot import (  # noqa: E402
    CONSUME_SQL,
    SNAPSHOT_SQL,
    PluginDatabase,
)
from mint_litellm.types.identity import OrgSource  # noqa: E402

fresh_db = ctl_fixtures.fresh_db
login_roles = ctl_fixtures.login_roles
CONFIRM = {"type": "require_user_confirmation", "purpose": "전송", "display_fields": ["q"]}


@pytest.fixture
def world(fresh_db, login_roles):
    scene = Scenario(fresh_db)
    params = conninfo_to_dict(fresh_db)
    params.update(user=LOGIN_ROLES["mint_plugin"], password=LOGIN_PASSWORDS["mint_plugin"])
    yield scene, make_conninfo(**params)
    scene.close()


def replica(scene, dsn, tmp_path, *, both=False):
    live = PluginDatabase(dsn, acquire_timeout=3)
    recorder = EvidenceRecorder(
        live, LocalBuffer(tmp_path / "buffer", 1024 * 1024), budget_seconds=5,
        instance_id=scene.iid, stderr=io.StringIO(), replay_interval_seconds=3600,
    )
    deps = PipelineDeps(
        config(tmp_path, instance_id=scene.iid, decision_budget_seconds=5), live, recorder,
        evaluator("RequireApproval" if both else "Allow", policy_ids=("R2",) if both else (),
                  obligations=(CONFIRM,), version="pv1", bundle="2" * 64), lambda: True,
    )
    return live, deps


async def ask(scene, deps, **kwargs):
    return await evaluate_request(request(user_principal(instance_id=scene.iid), **kwargs), deps)


def decide_as_staff(scene, *, token="console"):
    from datetime import UTC, datetime, timedelta

    with as_login(scene.url, "mint_staff") as sc, as_login(scene.url, "mint_control") as ctl:
        ctx = staff.StaffContext(sc, ctl, scene.iid, "u1", team_ids=("t1",))
        now = datetime.now(UTC)
        sid = uuid4().hex * 2
        staff.start_session(ctx, sid, token, now + timedelta(minutes=20), now + timedelta(hours=1))
        (cid,) = scene.conn.execute(
            "SELECT id FROM mint.self_confirmations WHERE instance_id = %s AND status = 'pending'",
            (scene.iid,),
        ).fetchone()
        staff.confirm(ctx, sid, str(cid))
        return cid


def test_two_replicas_create_one_pending_then_another_key_allows_once(world, tmp_path):
    scene, dsn = world

    async def go():
        a, da = replica(scene, dsn, tmp_path / "a")
        b, db = replica(scene, dsn, tmp_path / "b")
        try:
            blocked = await asyncio.gather(ask(scene, da), ask(scene, db))
            assert all(not o.allowed for o in blocked)
            assert scene.conn.execute(
                "SELECT count(*) FROM mint.self_confirmations WHERE instance_id = %s",
                (scene.iid,),
            ).fetchone() == (1,)
            decide_as_staff(scene)
            first = await ask(scene, da)
            second = await ask(scene, db)
            assert first.allowed and not second.allowed
            consumed = scene.conn.execute(
                "SELECT display, consumed_invocation_id FROM mint.self_confirmations"
                " WHERE instance_id = %s AND status = 'consumed'", (scene.iid,),
            ).fetchone()
            assert consumed == (None, first.snapshot.invocation_id)
            assert scene.conn.execute(
                "SELECT count(*) FROM mint.call_approvals WHERE instance_id = %s", (scene.iid,),
            ).fetchone() == (0,)
        finally:
            await a.close()
            await b.close()

    run(go())


def test_pending_limit_is_twenty_across_replicas_with_ten_minute_lifetime(world, tmp_path):
    scene, dsn = world

    async def go():
        a, da = replica(scene, dsn, tmp_path / "a")
        b, db = replica(scene, dsn, tmp_path / "b")
        try:
            await asyncio.gather(*(
                ask(scene, da if i % 2 else db, arguments={"q": str(i)}) for i in range(24)
            ))
        finally:
            await a.close()
            await b.close()

    run(go())
    rows = scene.conn.execute(
        "SELECT display, expires_at - created_at FROM mint.self_confirmations"
        " WHERE instance_id = %s", (scene.iid,),
    ).fetchall()
    assert len(rows) == 20
    assert all(599 <= lifetime.total_seconds() <= 605 for _, lifetime in rows)


@pytest.mark.parametrize("losing_table", ["confirmation", "call_approval"])
def test_one_failed_update_rolls_back_both_and_the_allow_event(world, tmp_path, losing_table):
    scene, dsn = world
    call_id = uuid4()
    scene.add_call_approval(call_id)

    async def go():
        live, deps = replica(scene, dsn, tmp_path)
        try:
            await ask(scene, deps)
            cid = decide_as_staff(scene)
            snap = await live.read_snapshot(scene.query())
            principal = user_principal(instance_id=scene.iid).model_copy(update={
                "org_id": "org1", "org_source": OrgSource.TEAM,
            })
            event = scene.event()
            invocation = event.invocation_id
            _, ca = match_call_approval(
                snap, principal, request().resolved, "search", {"q": "x"}, deps.config, invocation,
            )
            _, sc = match_confirmation(
                snap, principal, request().resolved, "search", {"q": "x"}, deps.config, invocation,
            )
            consume = replace(ca, confirmation=sc.confirmation)
            if losing_table == "confirmation":
                consume = replace(consume, confirmation=replace(sc.confirmation, state_version=0))
            else:
                consume = replace(consume, state_version=0)
            with pytest.raises(ConsumptionLost):
                await live.insert_event(event, consume)
            assert scene.conn.execute(
                "SELECT count(*) FROM mint.events WHERE event_id = %s", (event.event_id,),
            ).fetchone() == (0,)
            assert scene.conn.execute(
                "SELECT status FROM mint.self_confirmations WHERE id = %s", (cid,),
            ).fetchone() == ("confirmed",)
            assert scene.conn.execute(
                "SELECT status FROM mint.call_approvals WHERE id = %s", (call_id,),
            ).fetchone() == ("approved",)
        finally:
            await live.close()

    run(go())


@pytest.mark.parametrize("both", [False, True])
def test_two_replicas_with_the_same_snapshot_consume_once(world, tmp_path, both):
    scene, dsn = world
    if both:
        scene.add_call_approval(uuid4())

    async def go():
        a, da = replica(scene, dsn, tmp_path / "a", both=both)
        b, db = replica(scene, dsn, tmp_path / "b", both=both)
        try:
            await ask(scene, da)
            decide_as_staff(scene)
            barrier = asyncio.Barrier(2)

            class Together:
                def __init__(self, live):
                    self.live = live

                async def read_snapshot(self, query):
                    snap = await self.live.read_snapshot(query)
                    await barrier.wait()
                    return snap

            da.snapshots, db.snapshots = Together(a), Together(b)
            pair = await asyncio.gather(ask(scene, da), ask(scene, db))
            assert sorted(o.allowed for o in pair) == [False, True]
            reason = "MINT-CALL-APPROVAL-REQUIRED" if both else "MINT-CONFIRMATION-REQUIRED"
            assert [o.error.code for o in pair if not o.allowed] == [reason]
            ids = scene.conn.execute(
                "SELECT consumed_invocation_id FROM mint.call_approvals WHERE instance_id = %s"
                " UNION ALL SELECT consumed_invocation_id FROM mint.self_confirmations"
                " WHERE instance_id = %s", (scene.iid, scene.iid),
            ).fetchall()
            assert len(ids) == (2 if both else 1) and ids[0][0] is not None
            assert all(i == ids[0] for i in ids)
        finally:
            await a.close()
            await b.close()

    run(go())


def test_stage_one_queries_never_read_confirmation_rows():
    assert "self_confirmations" not in SNAPSHOT_SQL
    assert "self_confirmations" not in CONSUME_SQL
