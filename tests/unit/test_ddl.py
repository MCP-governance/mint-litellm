"""초기 스키마 시험. MINT_TEST_DATABASE_URL이 없으면 건너뛴다."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import errors  # noqa: E402

from mint_litellm.types.ids import new_uuid7  # noqa: E402
from mint_litellm.types.reason_codes import OBSERVATION_CANDIDATES  # noqa: E402

I = "inst1"  # noqa: E741
NOW = datetime.now(UTC)
HEX = "a" * 64


@contextmanager
def fails(conn, exc):
    with pytest.raises(exc), conn.transaction():
        yield


def epoch(conn, instance=I):
    row = conn.execute("SELECT epoch FROM mint.state_epoch WHERE instance_id = %s", (instance,))
    found = row.fetchone()
    return found[0] if found else 0


def scalar(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


def add_org(conn, org_id="orgA", instance=I):
    conn.execute(
        "INSERT INTO mint.orgs (instance_id, org_id, name) VALUES (%s, %s, %s)",
        (instance, org_id, org_id),
    )


def add_server(conn, server_id="srv-1", status="pending", instance=I):
    sid = new_uuid7()
    conn.execute(
        "INSERT INTO mint.servers (id, instance_id, server_id, status, url_sha256, transport,"
        " auth_type, registered_by) VALUES (%s, %s, %s, %s, %s, 'http', 'none', 'alice')",
        (sid, instance, server_id, status, HEX),
    )
    return sid


SERVER_PATH = {
    "pending": [],
    "validating": ["validating"],
    "awaiting_approval": ["validating", "awaiting_approval"],
    "rejected": ["validating", "rejected"],
    "approved": ["validating", "awaiting_approval", "approved"],
    "suspended": ["validating", "awaiting_approval", "approved", "suspended"],
    "terminating": ["validating", "awaiting_approval", "approved", "terminating"],
    "terminated": ["validating", "awaiting_approval", "approved", "terminating", "terminated"],
}


def set_server_status(conn, sid, status):
    conn.execute("UPDATE mint.servers SET status = %s WHERE id = %s", (status, sid))


def server_in(conn, status, server_id="srv-1"):
    add_org(conn)
    sid = add_server(conn, server_id)
    for step in SERVER_PATH[status]:
        set_server_status(conn, sid, step)
    return sid


def add_relation(conn, org="orgA", team="t1", server="srv-1", purpose="research"):
    rid = new_uuid7()
    conn.execute(
        "INSERT INTO mint.usage_relations (id, instance_id, org_id, team_id, purpose, server_id)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (rid, I, org, team, purpose, server),
    )
    return rid


def add_policy_version(conn, pv="pv-1"):
    conn.execute(
        "INSERT INTO mint.policy_versions (instance_id, id, catalog_sha256, bundle_sha256)"
        " VALUES (%s, %s, %s, %s)",
        (I, pv, HEX, HEX),
    )


@pytest.fixture
def base(db):
    add_org(db, "orgA")
    add_org(db, "orgB")
    server = add_server(db)
    relation = add_relation(db)
    add_policy_version(db)
    return {"server": server, "relation": relation}


def add_approval(conn, relation, status="active", org="orgA", server="srv-1", **override):
    aid = new_uuid7()
    row = {
        "id": aid,
        "instance_id": I,
        "org_id": org,
        "relation_id": relation,
        "class": "standard",
        "server_id": server,
        "tools": ["read"],
        "scope_type": "team",
        "scope_id": "t1",
        "valid_from": NOW,
        "valid_until": NOW + timedelta(days=30),
        "basis": '{"kind": "operator_registered"}',
        "granted_by": "alice" if status != "pending" else None,
        "requested_by": "op-1",
        "status": status,
        "revoked_at": NOW if status == "revoked" else None,
        **override,
    }
    cols = ", ".join(f'"{c}"' for c in row)
    marks = ", ".join(["%s"] * len(row))
    conn.execute(f"INSERT INTO mint.approvals ({cols}) VALUES ({marks})", list(row.values()))
    return aid


def add_call_approval(conn, fp=HEX):
    cid = new_uuid7()
    conn.execute(
        "INSERT INTO mint.call_approvals (id, instance_id, org_id, principal_type, principal_id,"
        " server_id, tool, args_fp, fp_key_version, policy_version_id, status, requested_by,"
        " expires_at) VALUES"
        " (%s, %s, 'orgA', 'user', 'u1', 'srv-1', 'write', %s, 1, 'pv-1', 'pending', 'op-1', %s)",
        (cid, I, fp, NOW + timedelta(minutes=15)),
    )
    return cid


def advance_call_approval(conn, cid, *steps, invocation=None):
    for step in steps:
        if step == "approved":
            conn.execute(
                "UPDATE mint.call_approvals SET status = 'approved', approved_by = 'alice'"
                " WHERE id = %s",
                (cid,),
            )
        elif step == "consumed":
            conn.execute(
                "UPDATE mint.call_approvals SET status = 'consumed', consumed_invocation_id = %s"
                " WHERE id = %s",
                (invocation or new_uuid7(), cid),
            )
        else:
            conn.execute("UPDATE mint.call_approvals SET status = %s WHERE id = %s", (step, cid))


EXPECTED_TABLES = {
    "orgs",
    "org_bindings",
    "servers",
    "usage_relations",
    "allowlists",
    "approvals",
    "denials",
    "call_approvals",
    "consents",
    "state_epoch",
    "events",
    "settings",
    "policy_versions",
    "operators",
}


def test_schema_has_the_fourteen_tables(db):
    rows = db.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = 'mint' AND table_type = 'BASE TABLE'"
    ).fetchall()
    assert {r[0] for r in rows} == EXPECTED_TABLES


# ---- 상태 전이 ----------------------------------------------------------------------------


def test_server_walks_the_full_lifecycle(db):
    add_org(db)
    sid = add_server(db, status="awaiting_approval")
    for step in ["approved", "suspended", "approved", "terminating", "terminated"]:
        set_server_status(db, sid, step)
    assert scalar(db, "SELECT status FROM mint.servers WHERE id = %s", sid) == "terminated"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("pending", "approved"),
        ("awaiting_approval", "suspended"),
        ("approved", "awaiting_approval"),
        ("suspended", "awaiting_approval"),
        ("rejected", "approved"),
        ("terminating", "approved"),
        ("terminated", "approved"),
        ("terminated", "pending"),
    ],
)
def test_server_rejects_illegal_transitions(db, source, target):
    sid = server_in(db, source)
    with fails(db, errors.CheckViolation):
        set_server_status(db, sid, target)


def test_server_cannot_be_created_as_approved(db):
    with fails(db, errors.CheckViolation):
        add_server(db, status="approved")


def test_server_identity_columns_are_frozen(db):
    sid = server_in(db, "pending")
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.servers SET server_id = 'other' WHERE id = %s", (sid,))


def test_relation_transitions(db, base):
    db.execute(
        "UPDATE mint.usage_relations SET status = 'terminating' WHERE id = %s", (base["relation"],)
    )
    db.execute(
        "UPDATE mint.usage_relations SET status = 'terminated' WHERE id = %s", (base["relation"],)
    )
    with fails(db, errors.CheckViolation):
        db.execute(
            "UPDATE mint.usage_relations SET status = 'active' WHERE id = %s", (base["relation"],)
        )


def test_approval_lifecycle_and_terminal_states(db, base):
    aid = add_approval(db, base["relation"])
    db.execute(
        "UPDATE mint.approvals SET status = 'revoked', revoked_at = %s, revoke_reason = 'x'"
        " WHERE id = %s",
        (NOW, aid),
    )
    with fails(db, errors.CheckViolation):
        db.execute(
            "UPDATE mint.approvals SET status = 'active', revoked_at = NULL WHERE id = %s", (aid,)
        )
    pending = add_approval(db, base["relation"], status="pending")
    db.execute(
        "UPDATE mint.approvals SET status = 'active', granted_by = 'alice' WHERE id = %s",
        (pending,),
    )
    expired = add_approval(db, base["relation"])
    db.execute("UPDATE mint.approvals SET status = 'expired' WHERE id = %s", (expired,))
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.approvals SET status = 'active' WHERE id = %s", (expired,))


def test_pending_approval_cannot_skip_to_revoked_or_expired(db, base):
    aid = add_approval(db, base["relation"], status="pending")
    with fails(db, errors.CheckViolation):
        db.execute(
            "UPDATE mint.approvals SET status = 'revoked', revoked_at = %s WHERE id = %s",
            (NOW, aid),
        )
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.approvals SET status = 'expired' WHERE id = %s", (aid,))


def test_approval_validity_cannot_be_extended(db, base):
    aid = add_approval(db, base["relation"])
    with fails(db, errors.CheckViolation):
        db.execute(
            "UPDATE mint.approvals SET valid_until = valid_until + interval '1 day' WHERE id = %s",
            (aid,),
        )
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.approvals SET tools = ARRAY['write'] WHERE id = %s", (aid,))


def test_approval_needs_a_grantor_unless_pending_and_a_valid_window(db, base):
    with fails(db, errors.CheckViolation):
        add_approval(db, base["relation"], granted_by=None)
    with fails(db, errors.CheckViolation):
        add_approval(db, base["relation"], valid_until=NOW - timedelta(days=1))
    with fails(db, errors.CheckViolation):
        add_approval(db, base["relation"], tools=[])


def test_revoked_status_requires_revoked_at(db, base):
    aid = add_approval(db, base["relation"])
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.approvals SET status = 'revoked' WHERE id = %s", (aid,))


def test_approval_cannot_borrow_a_relation_from_another_org_or_server(db, base):
    with fails(db, errors.ForeignKeyViolation):
        add_approval(db, base["relation"], org="orgB")
    add_server(db, server_id="srv-2")
    with fails(db, errors.ForeignKeyViolation):
        add_approval(db, base["relation"], server="srv-2")


def test_call_approval_state_machine(db, base):
    cid = add_call_approval(db)
    advance_call_approval(db, cid, "approved", "consumed")
    for target in ("approved", "pending", "cancelled", "expired", "rejected"):
        with fails(db, errors.CheckViolation):
            advance_call_approval(db, cid, target)


@pytest.mark.parametrize(
    ("path", "bad_next"),
    [
        (["rejected"], "approved"),
        (["cancelled"], "approved"),
        (["expired"], "consumed"),
        (["approved", "cancelled"], "approved"),
        (["approved", "expired"], "consumed"),
    ],
)
def test_call_approval_terminal_states(db, base, path, bad_next):
    cid = add_call_approval(db)
    advance_call_approval(db, cid, *path)
    with fails(db, errors.CheckViolation):
        advance_call_approval(db, cid, bad_next)


def test_call_approval_cannot_skip_approval(db, base):
    cid = add_call_approval(db)
    with fails(db, errors.CheckViolation):
        advance_call_approval(db, cid, "consumed")


def test_call_approval_fingerprint_is_frozen(db, base):
    cid = add_call_approval(db)
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.call_approvals SET args_fp = %s WHERE id = %s", ("b" * 64, cid))


def test_policy_version_chain_and_single_active(db, base):
    for step in ("staged", "active"):
        extra = ", activated_at = now(), activated_by = 'alice'" if step == "active" else ""
        db.execute(f"UPDATE mint.policy_versions SET status = %s{extra} WHERE id = 'pv-1'", (step,))
    add_policy_version(db, "pv-2")
    db.execute("UPDATE mint.policy_versions SET status = 'staged' WHERE id = 'pv-2'")
    with fails(db, errors.UniqueViolation):
        db.execute(
            "UPDATE mint.policy_versions SET status = 'active', activated_at = now(),"
            " activated_by = 'alice' WHERE id = 'pv-2'"
        )
    db.execute("UPDATE mint.policy_versions SET status = 'rolled_back' WHERE id = 'pv-1'")
    db.execute(
        "UPDATE mint.policy_versions SET status = 'active', activated_at = now(),"
        " activated_by = 'alice' WHERE id = 'pv-2'"
    )
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.policy_versions SET status = 'validated' WHERE id = 'pv-1'")


def test_policy_version_cannot_be_activated_without_staging(db, base):
    with fails(db, errors.CheckViolation):
        db.execute(
            "UPDATE mint.policy_versions SET status = 'active', activated_at = now(),"
            " activated_by = 'alice' WHERE id = 'pv-1'"
        )


def test_org_can_only_be_closed(db):
    add_org(db)
    db.execute("UPDATE mint.orgs SET status = 'closed' WHERE org_id = 'orgA'")
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.orgs SET status = 'active' WHERE org_id = 'orgA'")


def test_allowlist_requires_an_end_date_and_expires_one_way(db):
    row = (new_uuid7(), I, "provider", "example.com", NOW, NOW + timedelta(days=1), "alice")
    sql = (
        "INSERT INTO mint.allowlists (id, instance_id, kind, value, valid_from, valid_until,"
        " created_by) VALUES (%s, %s, %s, %s, %s, %s, %s)"
    )
    db.execute(sql, row)
    with fails(db, errors.NotNullViolation):
        db.execute(sql, (new_uuid7(), I, "provider", "b.example.com", NOW, None, "alice"))
    with fails(db, errors.CheckViolation):
        db.execute(
            sql,
            (new_uuid7(), I, "provider", "c.example.com", NOW, NOW - timedelta(days=1), "alice"),
        )
    with fails(db, errors.UniqueViolation):
        db.execute(sql, (new_uuid7(), *row[1:]))
    db.execute("UPDATE mint.allowlists SET status = 'lifted' WHERE id = %s", (row[0],))
    db.execute(sql, (new_uuid7(), *row[1:]))
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.allowlists SET status = 'active' WHERE id = %s", (row[0],))


# ---- 유일 제약 ---------------------------------------------------------------------------


def test_a_team_or_user_binds_to_one_org_only(db):
    add_org(db, "orgA")
    add_org(db, "orgB")
    sql = (
        "INSERT INTO mint.org_bindings (id, instance_id, org_id, subject_type, subject_id,"
        " created_by) VALUES (%s, %s, %s, %s, %s, 'alice')"
    )
    db.execute(sql, (new_uuid7(), I, "orgA", "team", "t1"))
    with fails(db, errors.UniqueViolation):
        db.execute(sql, (new_uuid7(), I, "orgB", "team", "t1"))
    db.execute(sql, (new_uuid7(), I, "orgB", "user", "t1"))


def test_binding_cannot_reference_an_org_of_another_instance(db):
    add_org(db, "orgA")
    with fails(db, errors.ForeignKeyViolation):
        db.execute(
            "INSERT INTO mint.org_bindings (id, instance_id, org_id, subject_type, subject_id,"
            " created_by) VALUES (%s, 'other', 'orgA', 'team', 't1', 'alice')",
            (new_uuid7(),),
        )


def test_server_id_is_unique_per_instance(db):
    add_server(db)
    with fails(db, errors.UniqueViolation):
        add_server(db)
    add_server(db, instance="inst2")


def test_relation_is_unique_even_without_a_team(db):
    add_org(db)
    add_server(db)
    add_relation(db, team=None)
    with fails(db, errors.UniqueViolation):
        add_relation(db, team=None)
    add_relation(db, team="t2")


def test_one_call_approval_is_consumed_per_invocation(db, base):
    invocation = new_uuid7()
    first = add_call_approval(db, fp="b" * 64)
    advance_call_approval(db, first, "approved", "consumed", invocation=invocation)
    second = add_call_approval(db, fp="c" * 64)
    advance_call_approval(db, second, "approved")
    with fails(db, errors.UniqueViolation):
        advance_call_approval(db, second, "consumed", invocation=invocation)
    advance_call_approval(db, second, "consumed")


def test_operator_token_is_unique(db):
    sql = (
        "INSERT INTO mint.operators (instance_id, name, role, token_hash, user_id)"
        " VALUES (%s, %s, 'admin', %s, 'u1')"
    )
    db.execute(sql, (I, "alice", HEX))
    with fails(db, errors.UniqueViolation):
        db.execute(sql, (I, "op-1", HEX))
    db.execute(sql, (I, "op-1", "b" * 64))


@pytest.mark.parametrize("role", ["admin", "approver"])
def test_admin_and_approver_require_a_user_binding(db, role):
    with fails(db, errors.CheckViolation):
        db.execute(
            "INSERT INTO mint.operators (instance_id, name, role, token_hash)"
            " VALUES (%s, 'x', %s, %s)",
            (I, role, HEX),
        )
    db.execute(
        "INSERT INTO mint.operators (instance_id, name, role, token_hash, user_id)"
        " VALUES (%s, 'y', %s, %s, 'u1')",
        (I, role, "b" * 64),
    )


def test_viewer_may_be_unbound(db):
    db.execute(
        "INSERT INTO mint.operators (instance_id, name, role, token_hash)"
        " VALUES (%s, 'v', 'viewer', %s)",
        (I, HEX),
    )


# ---- 동의 --------------------------------------------------------------------------------


def add_consent(conn, source="operator_attested", ref="ticket-1", sha=HEX, valid_days=30):
    conn.execute(
        "INSERT INTO mint.consents (id, instance_id, org_id, human_user_id, server_id, purpose,"
        " data_scope, text_version, source, evidence_ref, evidence_sha256, granted_at, valid_until,"
        " registered_by) VALUES (%s, %s, 'orgA', 'u1', 'srv-1', 'analytics', 'all', 'v1',"
        " %s, %s, %s, %s, %s, 'alice')",
        (new_uuid7(), I, source, ref, sha, NOW, NOW + timedelta(days=valid_days)),
    )


def test_operator_attested_consent_needs_evidence(db, base):
    add_consent(db)
    for kwargs in ({"ref": None}, {"sha": None}, {"ref": ""}, {"ref": None, "sha": None}):
        with fails(db, errors.CheckViolation):
            add_consent(db, **kwargs)


def test_self_service_consent_needs_no_evidence_but_hash_must_be_well_formed(db, base):
    add_consent(db, source="self_service", ref=None, sha=None)
    with fails(db, errors.CheckViolation):
        add_consent(db, source="self_service", ref=None, sha="not-a-hash")
    with fails(db, errors.CheckViolation):
        add_consent(db, source="other")
    with fails(db, errors.CheckViolation):
        add_consent(db, valid_days=-1)


def test_consent_revocation_is_one_way_and_terms_are_frozen(db, base):
    add_consent(db)
    cid = scalar(db, "SELECT id FROM mint.consents")
    db.execute("UPDATE mint.consents SET revoked_at = now() WHERE id = %s", (cid,))
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.consents SET revoked_at = NULL WHERE id = %s", (cid,))
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.consents SET purpose = 'ads' WHERE id = %s", (cid,))


def test_denial_lift_is_one_way(db, base):
    did = new_uuid7()
    db.execute(
        "INSERT INTO mint.denials (id, instance_id, scope_type, scope_id, reason, created_by)"
        " VALUES (%s, %s, 'user', 'u1', 'incident', 'alice')",
        (did, I),
    )
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.denials SET lifted_at = now() WHERE id = %s", (did,))
    db.execute(
        "UPDATE mint.denials SET lifted_at = now(), lifted_by = 'alice' WHERE id = %s", (did,)
    )
    with fails(db, errors.CheckViolation):
        db.execute(
            "UPDATE mint.denials SET lifted_at = NULL, lifted_by = NULL WHERE id = %s", (did,)
        )
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.denials SET scope_id = 'u2' WHERE id = %s", (did,))


def test_server_scoped_denial_must_name_the_server(db, base):
    sql = (
        "INSERT INTO mint.denials (id, instance_id, scope_type, scope_id, server_id, reason,"
        " created_by) VALUES (%s, %s, 'server', %s, %s, 'incident', 'alice')"
    )
    db.execute(sql, (new_uuid7(), I, "srv-1", "srv-1"))
    with fails(db, errors.CheckViolation):
        db.execute(sql, (new_uuid7(), I, "srv-1", None))
    with fails(db, errors.CheckViolation):
        db.execute(sql, (new_uuid7(), I, "srv-9", "srv-1"))


# ---- 세대와 버전 -------------------------------------------------------------------------


def test_every_enforcement_relevant_change_advances_the_epoch(db):
    assert epoch(db) == 0
    add_org(db)
    assert epoch(db) == 1
    sid = add_server(db, status="awaiting_approval")
    assert epoch(db) == 2
    set_server_status(db, sid, "approved")
    assert epoch(db) == 3
    db.execute("DELETE FROM mint.servers WHERE id = %s", (sid,))
    assert epoch(db) == 4


def test_epoch_moves_with_the_change_in_the_same_transaction(db, base):
    before = epoch(db)
    with db.transaction() as tx:
        add_approval(db, base["relation"])
        assert epoch(db) == before + 1
        raise psycopg.Rollback(tx)
    assert epoch(db) == before


def test_operators_do_not_move_the_epoch(db):
    db.execute(
        "INSERT INTO mint.operators (instance_id, name, role, token_hash)"
        " VALUES (%s, 'v', 'viewer', %s)",
        (I, HEX),
    )
    assert epoch(db) == 0


def test_epochs_are_per_instance(db):
    add_org(db, instance="inst2")
    assert epoch(db, "inst2") == 1 and epoch(db) == 0


def test_epoch_only_increases_and_is_never_deleted(db):
    add_org(db)
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.state_epoch SET epoch = epoch - 1")
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.state_epoch SET epoch = epoch")
    with fails(db, errors.CheckViolation):
        db.execute("DELETE FROM mint.state_epoch")
    db.execute("UPDATE mint.state_epoch SET epoch = epoch + 5")


def test_state_version_is_assigned_by_the_database(db):
    add_org(db)
    assert scalar(db, "SELECT state_version FROM mint.orgs") == 1
    db.execute("UPDATE mint.orgs SET name = 'renamed', state_version = 99")
    assert scalar(db, "SELECT state_version FROM mint.orgs") == 2


# ---- 설정 --------------------------------------------------------------------------------


def test_settings_defaults_and_checks(db):
    db.execute("INSERT INTO mint.settings (instance_id) VALUES (%s)", (I,))
    mode, reasons, policies, version = db.execute(
        "SELECT mode, observable_reasons, observable_policy_ids, settings_version"
        " FROM mint.settings"
    ).fetchone()
    assert mode == "enforce" and policies == [] and version == 1
    assert set(reasons) == {r.value for r in OBSERVATION_CANDIDATES}
    db.execute("UPDATE mint.settings SET mode = 'observe'")
    assert scalar(db, "SELECT settings_version FROM mint.settings") == 2
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.settings SET mode = 'off'")
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.settings SET observable_reasons = ARRAY['DENIED']")
    with fails(db, errors.ForeignKeyViolation):
        db.execute("UPDATE mint.settings SET default_org_id = 'missing'")
    add_org(db)
    db.execute("UPDATE mint.settings SET default_org_id = 'orgA'")


# ---- 원장 --------------------------------------------------------------------------------


def add_event(conn, key=None, kind="approval_granted", **cols):
    key = key or f"approval:{new_uuid7()}:1:{kind}"
    row = {
        "event_id": new_uuid7(),
        "instance_id": I,
        "kind": kind,
        "source_key": key,
        "actor": "alice",
        "payload": "{}",
        **cols,
    }
    names = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    cur = conn.execute(
        f"INSERT INTO mint.events ({names}) VALUES ({marks}) ON CONFLICT (source_key) DO NOTHING",
        list(row.values()),
    )
    return key, cur.rowcount


def test_event_source_key_converges_to_one_row(db):
    key, first = add_event(db)
    _, second = add_event(db, key=key)
    assert (first, second) == (1, 0)
    assert scalar(db, "SELECT count(*) FROM mint.events WHERE source_key = %s", key) == 1
    with fails(db, errors.UniqueViolation):
        db.execute(
            "INSERT INTO mint.events (event_id, instance_id, kind, source_key, payload)"
            " VALUES (%s, %s, 'x', %s, '{}')",
            (new_uuid7(), I, key),
        )


def test_call_events_require_their_identifiers(db):
    inv = new_uuid7()
    key = f"call:{inv}:1:decision_evaluated"
    with fails(db, errors.CheckViolation):
        add_event(db, key=key, kind="decision_evaluated")
    with fails(db, errors.CheckViolation):
        add_event(db, key=key, kind="decision_evaluated", invocation_id=inv)
    assert (
        add_event(db, key=key, kind="decision_evaluated", invocation_id=inv, attempt_id=1)[1] == 1
    )


def test_events_are_append_only(db):
    key, _ = add_event(db)
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.events SET payload = '{\"x\": 1}' WHERE source_key = %s", (key,))
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.events SET kind = 'other' WHERE source_key = %s", (key,))
    with fails(db, errors.CheckViolation):
        db.execute("DELETE FROM mint.events WHERE source_key = %s", (key,))
    with fails(db, errors.CheckViolation):
        db.execute("TRUNCATE mint.events")


def test_seal_columns_can_be_filled_exactly_once(db):
    key, _ = add_event(db)
    seal = (
        "UPDATE mint.events SET seal_seq = 1, prev_hash = NULL, row_hash = %s, sealed_at = now()"
        " WHERE source_key = %s"
    )
    with fails(db, errors.CheckViolation):
        db.execute("UPDATE mint.events SET seal_seq = 1 WHERE source_key = %s", (key,))
    db.execute(seal, (b"\x01" * 32, key))
    with fails(db, errors.CheckViolation):
        db.execute(seal, (b"\x02" * 32, key))


# ---- 역할 분리 ---------------------------------------------------------------------------


def test_plugin_role_reads_state_and_consumes_call_approvals_only(db, base):
    approver_ready = add_call_approval(db)
    advance_call_approval(db, approver_ready, "approved")
    before = epoch(db)
    db.execute("SET LOCAL ROLE mint_plugin")
    assert scalar(db, "SELECT count(*) FROM mint.approvals") == 0
    add_event(db)
    invocation = new_uuid7()
    db.execute(
        "UPDATE mint.call_approvals SET status = 'consumed', consumed_invocation_id = %s"
        " WHERE id = %s",
        (invocation, approver_ready),
    )
    for denied in (
        "UPDATE mint.approvals SET status = 'revoked'",
        "UPDATE mint.call_approvals SET approved_by = 'mallory'",
        "UPDATE mint.call_approvals SET args_fp = repeat('c', 64)",
        "INSERT INTO mint.approvals (id) VALUES (gen_random_uuid())",
        "DELETE FROM mint.denials",
        "UPDATE mint.state_epoch SET epoch = epoch + 1",
        "SELECT * FROM mint.operators",
        "UPDATE mint.events SET kind = 'x'",
    ):
        with fails(db, errors.InsufficientPrivilege):
            db.execute(denied)
    db.execute("RESET ROLE")
    assert epoch(db) == before + 1
    assert (
        scalar(db, "SELECT state_version FROM mint.call_approvals WHERE id = %s", approver_ready)
        == 3
    )


def test_control_role_changes_state_but_not_the_ledger_or_epoch(db, base):
    db.execute("SET LOCAL ROLE mint_control")
    add_approval(db, base["relation"])
    add_event(db)
    db.execute(
        "INSERT INTO mint.operators (instance_id, name, role, token_hash)"
        " VALUES (%s, 'v', 'viewer', %s)",
        (I, HEX),
    )
    for denied in (
        "UPDATE mint.state_epoch SET epoch = epoch + 1",
        "DELETE FROM mint.events",
        "UPDATE mint.events SET seal_seq = 1",
        "DELETE FROM mint.approvals",
    ):
        with fails(db, errors.InsufficientPrivilege):
            db.execute(denied)


def test_sealer_role_can_only_fill_seal_columns(db, base):
    key, _ = add_event(db)
    db.execute("SET LOCAL ROLE mint_sealer")
    assert scalar(db, "SELECT count(*) FROM mint.events") == 1
    with fails(db, errors.InsufficientPrivilege):
        db.execute("UPDATE mint.events SET payload = '{}'")
    with fails(db, errors.InsufficientPrivilege):
        db.execute("SELECT * FROM mint.approvals")
    with fails(db, errors.InsufficientPrivilege):
        add_event(db)
    db.execute(
        "UPDATE mint.events SET seal_seq = 1, row_hash = %s, sealed_at = now()"
        " WHERE source_key = %s",
        (b"\x03" * 32, key),
    )
