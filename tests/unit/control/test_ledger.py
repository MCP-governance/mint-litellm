"""원장 삽입·조회와 배치 범위 유일성. MINT_TEST_DATABASE_URL이 없으면 건너뛴다."""

from datetime import UTC, datetime, timedelta

import pytest
from ctl_fixtures import INSTANCE

psycopg = pytest.importorskip("psycopg")

from mint_litellm.control import ledger  # noqa: E402
from mint_litellm.control.db.connection import fetchone  # noqa: E402
from mint_litellm.types.events import EventKind  # noqa: E402


def record(conn, instance=INSTANCE, entity="alice", version=1, kind=EventKind.OPERATOR_ADDED):
    return ledger.record(
        conn,
        instance,
        kind,
        entity_id=entity,
        state_version=version,
        actor="tester",
        payload={"name": entity},
        subject={"x": 1},
    )


def test_record_inserts_one_row_with_the_control_source_key(conn):
    event = record(conn)
    row = fetchone(conn, "SELECT * FROM mint.events WHERE event_id = %s", (event.event_id,))
    assert row["source_key"] == "operator:alice:1:operator_added"
    assert row["actor"] == "tester" and row["payload"] == {"name": "alice"}
    assert row["subject"] == {"x": 1}
    assert row["seal_seq"] is None


def test_the_same_event_key_in_two_instances_is_two_rows(conn):
    record(conn, "inst1")
    record(conn, "inst2")
    sql = (
        "SELECT count(*) AS n FROM mint.events"
        " WHERE source_key = 'operator:alice:1:operator_added'"
    )
    assert fetchone(conn, sql)["n"] == 2


def test_replaying_the_same_event_in_an_instance_is_refused(conn):
    record(conn)
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        record(conn)


def test_list_events_filters(conn):
    record(conn, entity="a")
    record(conn, entity="b", kind=EventKind.ORG_CREATED)
    now = datetime.now(UTC)
    assert len(ledger.list_events(conn, INSTANCE)) == 2
    only = ledger.list_events(conn, INSTANCE, kind="org_created")
    assert [r["source_key"] for r in only] == ["org:b:1:org_created"]
    assert ledger.list_events(conn, INSTANCE, since=now + timedelta(hours=1)) == []
    assert len(ledger.list_events(conn, INSTANCE, until=now + timedelta(hours=1), limit=1)) == 1
    assert ledger.list_events(conn, "other") == []
