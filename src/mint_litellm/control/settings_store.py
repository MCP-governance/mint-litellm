"""저장된 설정(모드, 관측 가능 사유, 기본 조직)."""

import psycopg

from mint_litellm.control import orgs
from mint_litellm.control.context import Context
from mint_litellm.control.db.connection import fetchone
from mint_litellm.control.errors import ControlError
from mint_litellm.types.events import EventKind
from mint_litellm.types.reason_codes import OBSERVATION_CANDIDATES
from mint_litellm.types.settings import Mode, effective_mode

# 1단계 기본 정책 가운데 관측 모드로 전환할 수 있다고 선언된 정책
DEFAULT_OBSERVABLE_POLICY_IDS = ("R1", "R3")


def ensure(conn: psycopg.Connection, instance_id: str) -> int | None:
    """설정 행이 없으면 만들고 새 ``settings_version``을, 이미 있으면 None을 돌려준다."""
    row = fetchone(
        conn,
        "INSERT INTO mint.settings (instance_id, observable_policy_ids) VALUES (%s, %s)"
        " ON CONFLICT (instance_id) DO NOTHING RETURNING settings_version",
        (instance_id, list(DEFAULT_OBSERVABLE_POLICY_IDS)),
    )
    return None if row is None else row["settings_version"]


def load(conn: psycopg.Connection, instance_id: str) -> dict:
    row = fetchone(conn, "SELECT * FROM mint.settings WHERE instance_id = %s", (instance_id,))
    if row is None:
        raise ControlError("설정이 없습니다. init을 먼저 실행하십시오.")
    return row


def _changed(ctx: Context, row: dict, what: dict) -> None:
    ctx.emit(EventKind.SETTINGS_CHANGED, ctx.instance_id, row["settings_version"], what)


def set_default_org(ctx: Context, org_id: str | None) -> None:
    load(ctx.conn, ctx.instance_id)
    if org_id is not None:
        orgs.require_active(ctx, org_id)
    row = fetchone(
        ctx.conn,
        "UPDATE mint.settings SET default_org_id = %s WHERE instance_id = %s"
        " RETURNING settings_version",
        (org_id, ctx.instance_id),
    )
    _changed(ctx, row, {"default_org_id": org_id})


def set_mode(ctx: Context, mode: Mode, ceiling: Mode) -> None:
    load(ctx.conn, ctx.instance_id)
    if effective_mode(ceiling, mode) is not mode:
        raise ControlError("환경 변수 MINT_MODE가 enforce이므로 observe로 바꿀 수 없습니다.")
    row = fetchone(
        ctx.conn,
        "UPDATE mint.settings SET mode = %s WHERE instance_id = %s RETURNING settings_version",
        (mode.value, ctx.instance_id),
    )
    _changed(ctx, row, {"mode": mode.value})


def set_observable(
    ctx: Context, reasons: list[str] | None, policy_ids: list[str] | None
) -> None:
    current = load(ctx.conn, ctx.instance_id)
    if reasons is None and policy_ids is None:
        raise ControlError("--reasons 또는 --policy-ids 중 하나는 지정해야 합니다.")
    allowed = {r.value for r in OBSERVATION_CANDIDATES}
    unknown = sorted(set(reasons or []) - allowed)
    if unknown:
        raise ControlError(
            f"관측으로 전환할 수 없는 사유 계열입니다: {', '.join(unknown)}. "
            f"가능한 값: {', '.join(sorted(allowed))}"
        )
    new_reasons = current["observable_reasons"] if reasons is None else sorted(set(reasons))
    new_ids = current["observable_policy_ids"] if policy_ids is None else sorted(set(policy_ids))
    row = fetchone(
        ctx.conn,
        "UPDATE mint.settings SET observable_reasons = %s, observable_policy_ids = %s"
        " WHERE instance_id = %s RETURNING settings_version",
        (new_reasons, new_ids, ctx.instance_id),
    )
    _changed(ctx, row, {"observable_reasons": new_reasons, "observable_policy_ids": new_ids})
