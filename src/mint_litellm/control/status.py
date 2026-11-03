"""``status``: 모드, 정책 판본, 경보, 관측 항목."""

from datetime import timedelta
from typing import Any

from mint_litellm.control import approvals, checks, settings_store
from mint_litellm.control.config import ControlConfig
from mint_litellm.control.context import Context
from mint_litellm.control.db.connection import fetchall, fetchone
from mint_litellm.control.litellm_admin import LiteLLMAdmin
from mint_litellm.types.events import AlertKind
from mint_litellm.types.settings import OBSERVATION_ITEMS, Mode, effective_mode

EXPIRY_WARNING = timedelta(days=7)


def collect(
    ctx: Context,
    cfg: ControlConfig,
    litellm: LiteLLMAdmin | None,
    config: dict | None,
    env: dict[str, str],
) -> dict[str, Any]:
    stored = settings_store.load(ctx.conn, ctx.instance_id)
    active = fetchone(
        ctx.conn,
        "SELECT id, activated_at FROM mint.policy_versions"
        " WHERE instance_id = %s AND status = 'active'",
        (ctx.instance_id,),
    )
    servers = {
        r["status"]: r["n"]
        for r in fetchall(
            ctx.conn,
            "SELECT status, count(*) AS n FROM mint.servers WHERE instance_id = %s GROUP BY status",
            (ctx.instance_id,),
        )
    }
    alerts: list[dict[str, Any]] = [
        {
            "kind": AlertKind.APPROVAL_EXPIRING.value,
            "approval_id": str(a["id"]),
            "server_id": a["server_id"],
            "valid_until": a["valid_until"].isoformat(),
        }
        for a in approvals.expiring_approvals(ctx, EXPIRY_WARNING)
    ]
    observations: dict[str, Any] = {}
    if litellm is not None:
        report = checks.run_checks(config, env, litellm, replicas=cfg.replicas,
                                   compat_probe=lambda: None)
        observations = report.observations
        names: dict[str, int] = {}
        for entry in litellm.list_servers():
            for label in {entry.get("server_name"), entry.get("alias")} - {None, ""}:
                names[label] = names.get(label, 0) + 1
        alerts.extend(
            {"kind": AlertKind.SERVER_IDENTITY_CONFLICT.value, "name": name}
            for name, count in sorted(names.items()) if count > 1
        )
    elif config is not None:
        observations = checks.run_checks(config, env, None, compat_probe=lambda: None).observations
    ceiling = cfg.mode_ceiling
    return {
        "instance_id": ctx.instance_id,
        "mode": {
            "stored": stored["mode"],
            "ceiling": ceiling.value,
            "effective": effective_mode(ceiling, Mode(stored["mode"])).value,
        },
        "default_org_id": stored["default_org_id"],
        "policy_version": None if active is None else active["id"],
        "servers": servers,
        "alerts": alerts,
        "observations": {name: observations.get(name, "확인 불가") for name in OBSERVATION_ITEMS},
    }


def render(info: dict[str, Any]) -> str:
    mode = info["mode"]
    lines = [
        f"배치: {info['instance_id']}",
        f"모드: {mode['effective']} (저장 {mode['stored']}, 환경 상한 {mode['ceiling']})",
        f"기본 조직: {info['default_org_id'] or '없음'}",
        f"정책 판본: {info['policy_version'] or '없음'}",
        "서버: " + (", ".join(f"{k} {v}" for k, v in sorted(info["servers"].items())) or "없음"),
        "경보: " + ("없음" if not info["alerts"] else ""),
    ]
    for alert in info["alerts"]:
        detail = {k: v for k, v in alert.items() if k != "kind"}
        lines.append(f"  {alert['kind']} {detail}")
    lines.append("관측 항목")
    lines.extend(f"  {k}: {v}" for k, v in info["observations"].items())
    return "\n".join(lines)
