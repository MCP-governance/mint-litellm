"""복원 확인·해제: ``restore check|attest|accept-stale``.

복원 명령은 파일을 쓰지 않는다. ``attest``는 DB에서 누락된 부정 상태를 파일의 행 스냅숏대로
되돌리고 세대를 파일 세대 + 1로 올린 뒤 복원 요청과 사건을 커밋하고 작업 ``restore_publish``를
등록한다. ``accept-stale``(admin 2인)은 파일이 없거나 검증되지 않을 때 복원 요청과 사건만 커밋하고
같은 작업을 등록한다. 파일 보존·게시·검증은 작업 실행기(``control/restore_publish.py``)가 한다.
"""

from typing import Any

import psycopg

from mint_litellm.control import exports
from mint_litellm.control.context import Context, Operator
from mint_litellm.control.db.connection import fetchall, fetchone
from mint_litellm.control.errors import ControlError, forbidden, separation_of_duties
from mint_litellm.control.services import restore_rows
from mint_litellm.control.services.base import enqueue_job
from mint_litellm.control.stage2_config import Stage2Settings
from mint_litellm.restore import judge
from mint_litellm.types.events import EventKind
from mint_litellm.types.ids import new_uuid7


def _admin(ctx: Context) -> None:
    if ctx.operator.role != "admin":
        raise forbidden(f"복원 명령은 {ctx.operator.role} 역할로 실행할 수 없습니다.", exit_code=4)


def _read(settings: Stage2Settings, instance_id: str) -> judge.FileState:
    return judge.read_files(settings.export_dir, instance_id, exports.keys(settings))


def compare(conn: psycopg.Connection, instance_id: str, items: list[dict]) -> list[dict]:
    """파일의 항목마다 DB의 현재 값과 비교해 어긋난 것만 돌려준다."""
    found = []
    for item in items:
        current = restore_rows._current(conn, instance_id, item["kind"], item["id"])
        if current is None:
            found.append({**item, "problem": "missing_row"})
            continue
        _table, _column, status, *_ = restore_rows._META[item["kind"]]
        if status is not None:
            state = current[status]
        elif item["kind"] == "denial":
            state = "active" if current["lifted_at"] is None else "lifted"
        else:
            state = "active" if current["revoked_at"] is None else "revoked"
        if state != item["state"] or current["state_version"] < item["state_version"]:
            found.append({**item, "problem": "reapply", "db_state": state})
    return found


def _brief(problem: dict) -> dict:
    return {k: v for k, v in problem.items() if k != "row"}


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
        "items": [_brief(p) for p in compare(conn, instance_id, negative.get("items") or [])],
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


def _request(
    ctx: Context, kind: str, reason: str, file_epoch: int | None, target: int,
    cause: str | None, second: str | None,
) -> str:
    request_id = str(new_uuid7())
    ctx.conn.execute(
        "INSERT INTO mint.restore_requests (id, instance_id, kind, reason, requested_by,"
        " second_approver, file_epoch, target_epoch, cause) VALUES (%s, %s, %s, %s, %s, %s, %s,"
        " %s, %s)",
        (request_id, ctx.instance_id, kind, reason, ctx.operator.name, second, file_epoch,
         target, cause),
    )
    enqueue_job(ctx, "restore_publish", request_id, f"restore_publish:{request_id}")
    return request_id


def _second_admin(ctx: Context, second: Operator | None) -> Operator:
    if (
        second is None
        or second.role != "admin"
        or second.user_id in (None, ctx.operator.user_id)
    ):
        raise separation_of_duties(
            "다른 사용자에 결속된 admin의 두 번째 승인(--second-approver)이 필요합니다."
        )
    return second


def attest(
    ctx: Context, settings: Stage2Settings, reason: str, second: Operator | None = None
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
    rank = {kind: i for i, kind in enumerate(restore_rows.ORDER)}
    reapplied, failed = [], []
    pending = compare(ctx.conn, ctx.instance_id, items)
    cause, second_name = None, None
    if not pending:  # 되돌릴 부정 상태가 없으면 봉인 앵커 불일치를 인정하는 일뿐이므로 2인이다
        facts = fetchone(ctx.conn, judge.FACTS_SQL, judge.facts_params(state, ctx.instance_id))
        if not facts["anchor_ok"]:
            second_name = _second_admin(ctx, second).name
            cause = "anchor"
    with ctx.conn.transaction():
        for problem in sorted(pending, key=lambda p: rank[p["kind"]]):
            try:
                with ctx.conn.transaction():
                    restore_rows.restore_item(ctx.conn, ctx.instance_id, problem)
                reapplied.append({"kind": problem["kind"], "id": problem["id"],
                                  "was": problem["problem"]})
            except (psycopg.Error, ValueError) as error:
                failed.append({"kind": problem["kind"], "id": problem["id"],
                               "reason": type(error).__name__})
        if failed:
            raise ControlError(
                "되돌리지 못한 부정 상태가 있습니다(부모 행이 없거나 되돌릴 수 없는 전이): "
                + ", ".join(f"{f['kind']}:{f['id']}({f['reason']})" for f in failed)
                + ". 부모 행이 없으면 restore accept-stale(admin 2인)을 쓰십시오."
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
        request_id = _request(ctx, "attest", reason, file_epoch, current, cause, second_name)
        ctx.emit(EventKind.RESTORE_ATTESTED, file_epoch,
                 _sequence(ctx.conn, ctx.instance_id, file_epoch, EventKind.RESTORE_ATTESTED),
                 {"reason": reason, "file_epoch": file_epoch, "db_epoch": current,
                  "request_id": request_id, "reapplied": reapplied,
                  "second_approver": second_name})
    return {"file_epoch": file_epoch, "db_epoch": current, "reapplied": reapplied,
            "request_id": request_id}


def accept_stale(
    ctx: Context, settings: Stage2Settings, reason: str, second: Operator | None
) -> dict[str, Any]:
    _admin(ctx)
    if not reason.strip():
        raise ControlError("--reason이 필요합니다.")
    second = _second_admin(ctx, second)
    state = _read(settings, ctx.instance_id)
    if state.error not in ("missing", "unverified"):
        raise ControlError(
            "파일이 없거나 검증되지 않는 경우에만 쓸 수 있습니다. "
            "내용 불일치는 restore attest를 쓰십시오."
        )
    with ctx.conn.transaction():
        current = fetchone(
            ctx.conn, "SELECT coalesce(max(epoch), 0) AS e FROM mint.state_epoch"
            " WHERE instance_id = %s", (ctx.instance_id,))["e"]
        # 세대를 올려 새 게시가 새 ``exports`` 행(새 해시·봉인 끝)을 갖게 한다
        epoch = current + 1
        ctx.conn.execute("SELECT mint.restore_attest_epoch(%s, %s)", (ctx.instance_id, epoch))
        request_id = _request(ctx, "stale_accepted", reason, None, epoch, state.error, second.name)
        ctx.emit(EventKind.RESTORE_STALE_ACCEPTED, epoch,
                 _sequence(ctx.conn, ctx.instance_id, epoch, EventKind.RESTORE_STALE_ACCEPTED),
                 {"reason": reason, "second_approver": second.name, "cause": state.error,
                  "request_id": request_id})
    return {"cause": state.error, "db_epoch": epoch, "request_id": request_id}
