"""복원 확인·해제: ``restore check|attest|accept-stale``.

``check``는 파일의 부정 상태와 DB를 비교해 보여 준다. ``attest``는 누락된 부정 상태를 다시 적용하고
세대를 파일 세대 + 1로 올린 뒤 사건을 남기고 DB 기준 새 파일을 쓴다.
``accept-stale``은 파일이 없거나
검증되지 않을 때 admin 2인이 DB 기준 새 ``latest.json``을 쓰고 해제한다. 증거로 남은 옛 파일은
지우지 않고 이름만 바꿔 둔다.
"""

import shutil
from datetime import UTC, datetime
from typing import Any

import psycopg

from mint_litellm.control import exports
from mint_litellm.control.context import Context, Operator
from mint_litellm.control.db.connection import fetchall, fetchone
from mint_litellm.control.errors import ControlError, forbidden, separation_of_duties
from mint_litellm.control.stage2_config import Stage2Settings
from mint_litellm.restore import judge
from mint_litellm.types.events import EventKind

# 항목 종류 -> (표, 식별 열, 현재 상태 식)
_DENIAL = "CASE WHEN lifted_at IS NULL THEN 'active' ELSE 'lifted' END"
_CONSENT = "CASE WHEN revoked_at IS NULL THEN 'active' ELSE 'revoked' END"
_ITEMS = {
    "approval": ("approvals", "id::text", "status"),
    "denial": ("denials", "id::text", _DENIAL),
    "server": ("servers", "server_id", "status"),
    "relation": ("usage_relations", "id::text", "status"),
    "org": ("orgs", "org_id", "status"),
    "allowlist": ("allowlists", "id::text", "status"),
    "consent": ("consents", "id::text", _CONSENT),
}
# 다시 적용하는 문장. 거부 해제는 되돌릴 수 없어 적용 대상이 아니다.
_WHERE = " WHERE instance_id = %s AND "
_REAPPLY = {
    "approval": "UPDATE mint.approvals SET status = 'revoked', revoked_at = now()"
                + _WHERE + "id::text = %s",
    "server": "UPDATE mint.servers SET status = %s" + _WHERE + "server_id = %s",
    "relation": "UPDATE mint.usage_relations SET status = %s" + _WHERE + "id::text = %s",
    "org": "UPDATE mint.orgs SET status = %s" + _WHERE + "org_id = %s",
    "allowlist": "UPDATE mint.allowlists SET status = 'lifted'" + _WHERE + "id::text = %s",
    "consent": "UPDATE mint.consents SET revoked_at = now()" + _WHERE + "id::text = %s",
}
_DENIAL_COLUMNS = ("org_id", "scope_type", "scope_id", "server_id", "tools", "reason", "created_by")
_RECREATE_DENIAL = (
    "INSERT INTO mint.denials (id, instance_id, org_id, scope_type, scope_id, server_id, tools,"
    " reason, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_STATE_PARAM = {"server", "relation", "org"}


def _admin(ctx: Context) -> None:
    if ctx.operator.role != "admin":
        raise forbidden(f"복원 명령은 {ctx.operator.role} 역할로 실행할 수 없습니다.", exit_code=4)


def _read(settings: Stage2Settings, instance_id: str) -> judge.FileState:
    return judge.read_files(settings.export_dir, instance_id, exports.keys(settings))


def compare(conn: psycopg.Connection, instance_id: str, items: list[dict]) -> list[dict]:
    """파일의 항목마다 DB의 현재 값과 비교해 어긋난 것만 돌려준다(``problem``: 종류별 사유)."""
    found = []
    for item in items:
        table, column, state = _ITEMS[item["kind"]]
        row = fetchone(
            conn,
            f"SELECT state_version, {state} AS state FROM mint.{table}"
            f" WHERE instance_id = %s AND {column} = %s",
            (instance_id, item["id"]),
        )
        if row is None:
            # 없음이 곧 차단인 종류는 효력 없음, 거부는 없으면 허용이 되므로 다시 만들어야 한다
            problem = "missing_row" if item["kind"] == "denial" else "absent_no_effect"
            found.append({**item, "problem": problem})
        elif row["state"] != item["state"]:
            found.append({**item, "problem": "reapply", "db_state": row["state"]})
    return found


def check(conn: psycopg.Connection, instance_id: str, settings: Stage2Settings) -> dict[str, Any]:
    state = _read(settings, instance_id)
    if state.error is not None:
        return {"readable": False, "error": state.error}
    latest, negative = state.latest, state.negative or {}
    facts = fetchone(conn, judge.FACTS_SQL, judge.facts_params(state, instance_id))
    verdict = judge.Judge().decide(state, facts)
    since = latest["written_at"]
    activity = fetchall(
        conn,
        "SELECT DISTINCT subject ->> 'server_id' AS server_id FROM mint.events"
        " WHERE instance_id = %s AND inserted_at >= %s::timestamptz"
        " AND subject ->> 'server_id' IS NOT NULL ORDER BY 1",
        (instance_id, since),
    )
    return {
        "readable": True,
        "file_epoch": int(latest["state_epoch"]),
        "db_epoch": facts["epoch"] or 0,
        "last_export_at": since,
        "anchor_ok": bool(facts["anchor_ok"]),
        "mismatch": verdict.state != judge.OK,
        "items": compare(conn, instance_id, negative.get("items") or []),
        # 마지막 성공 전송 이후 구간에 활동이 있었던 서버: LiteLLM 권한의 현재값을 점검한다
        "review_servers": [r["server_id"] for r in activity],
    }


def _sequence(conn: psycopg.Connection, instance_id: str, epoch: int, kind: EventKind) -> int:
    return fetchone(
        conn,
        "SELECT count(*) + 1 AS n FROM mint.events WHERE instance_id = %s AND kind = %s"
        " AND source_key LIKE %s",
        (instance_id, kind.value, f"restore:{epoch}:%"),
    )["n"]


def _record(ctx: Context, kind: EventKind, epoch: int, payload: dict[str, Any]) -> None:
    ctx.emit(kind, epoch, _sequence(ctx.conn, ctx.instance_id, epoch, kind), payload)


def attest(
    ctx: Context, settings: Stage2Settings, reason: str
) -> dict[str, Any]:
    _admin(ctx)
    if not reason.strip():
        raise ControlError("--reason이 필요합니다.")
    state = _read(settings, ctx.instance_id)
    if state.error is not None or state.latest is None:
        raise ControlError(
            "내보내기 파일을 읽을 수 없습니다. restore accept-stale로 해제하십시오."
        )
    file_epoch = int(state.latest["state_epoch"])
    items = (state.negative or {}).get("items") or []
    reapplied, failed, absent = [], [], []
    with ctx.conn.transaction():
        for problem in compare(ctx.conn, ctx.instance_id, items):
            kind, ident = problem["kind"], problem["id"]
            if problem["problem"] == "absent_no_effect":
                absent.append({"kind": kind, "id": ident, "note": "복구 DB에 없음(효력 없음)"})
                continue
            if problem["problem"] == "missing_row":  # 거부: 같은 id로 다시 만든다
                spec = problem.get("recreate")
                try:
                    if spec is None:
                        raise ValueError
                    with ctx.conn.transaction():
                        ctx.conn.execute(_RECREATE_DENIAL, (ident, ctx.instance_id, *(
                            spec[k] for k in _DENIAL_COLUMNS)))
                    reapplied.append({"kind": kind, "id": ident, "recreated": True})
                except (psycopg.Error, ValueError, KeyError):
                    failed.append({**problem, "problem": "not_applicable"})
                continue
            if kind not in _REAPPLY:
                failed.append(problem)
                continue
            params = (ctx.instance_id, ident)
            if kind in _STATE_PARAM:
                params = (problem["state"], ctx.instance_id, ident)
            try:
                with ctx.conn.transaction():
                    ctx.conn.execute(_REAPPLY[kind], params)
                reapplied.append({"kind": kind, "id": ident})
            except psycopg.Error:
                failed.append({**problem, "problem": "not_applicable"})
        if failed:
            raise ControlError(
                "다시 적용하지 못한 부정 상태가 있습니다(행이 없거나 되돌릴 수 없는 전이): "
                + ", ".join(f"{f['kind']}:{f['id']}" for f in failed)
                + ". 같은 항목을 일반 명령으로 먼저 등록한 뒤 다시 실행하십시오."
            )
        target = file_epoch + 1
        current = fetchone(
            ctx.conn, "SELECT epoch FROM mint.state_epoch WHERE instance_id = %s",
            (ctx.instance_id,),
        )["epoch"]
        while current < target:  # 한 번에 1,000까지만 올릴 수 있다
            current = min(current + 1000, target)
            ctx.conn.execute(
                "SELECT mint.restore_attest_epoch(%s, %s)", (ctx.instance_id, current)
            )
        _record(ctx, EventKind.RESTORE_ATTESTED, file_epoch,
                {"reason": reason, "file_epoch": file_epoch, "db_epoch": current,
                 "reapplied": reapplied, "absent": absent})
    path = exports.write_files(ctx.conn, ctx.instance_id, settings)
    return {"file_epoch": file_epoch, "db_epoch": current, "reapplied": reapplied,
            "absent": absent, "export": path}


def accept_stale(
    ctx: Context, settings: Stage2Settings, reason: str, second: Operator | None
) -> dict[str, Any]:
    _admin(ctx)
    if not reason.strip():
        raise ControlError("--reason이 필요합니다.")
    if (
        second is None
        or second.role != "admin"
        or second.user_id in (None, ctx.operator.user_id)
    ):
        raise separation_of_duties(
            "accept-stale은 다른 사용자에 결속된 admin의 두 번째 승인"
            "(--second-approver)이 필요합니다."
        )
    state = _read(settings, ctx.instance_id)
    if state.error not in ("missing", "unverified"):
        raise ControlError(
            "파일이 없거나 검증되지 않는 경우에만 쓸 수 있습니다. "
            "내용 불일치는 restore attest를 쓰십시오."
        )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = settings.export_dir / ctx.instance_id
    for name in ("latest.json", "negative"):  # 증거는 지우지 않고 이름만 바꾼다
        path = root / name
        if path.exists():
            shutil.move(str(path), str(root / f"{name}.stale-{stamp}"))
    snap = exports.snapshot(ctx.conn, ctx.instance_id)
    with ctx.conn.transaction():
        _record(ctx, EventKind.RESTORE_STALE_ACCEPTED, snap["epoch"],
                {"reason": reason, "second_approver": second.name, "cause": state.error})
    path = exports.write_files(ctx.conn, ctx.instance_id, settings, force_latest=True)
    return {"cause": state.error, "db_epoch": snap["epoch"],
            "latest": path}
