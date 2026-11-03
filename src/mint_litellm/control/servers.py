"""서버 등록과 중지·재개.

서버의 계약 값(URL 해시·전송 방식·인증 방식)은 등록 시점의 LiteLLM 레지스트리 값이다.
호출 시점의 레지스트리 값과 하나라도 다르면 플러그인이 호출을 차단한다.
"""

from mint_litellm.control.context import Context
from mint_litellm.control.db.connection import fetchone
from mint_litellm.control.errors import ControlError
from mint_litellm.control.litellm_admin import LiteLLMAdmin
from mint_litellm.types.decision import normalize_auth_type, url_sha256
from mint_litellm.types.events import EventKind
from mint_litellm.types.ids import new_uuid7


def register(ctx: Context, litellm: LiteLLMAdmin, server_id: str, url: str | None = None) -> None:
    entry = litellm.get_server(server_id)
    if entry is None:
        raise ControlError(f"LiteLLM에 서버 {server_id}이(가) 없습니다.")
    if entry.get("approval_status") not in (None, "active", "approved"):
        raise ControlError(
            f"서버 {server_id}는 LiteLLM에서 아직 승인되지 않았습니다"
            f"(approval_status={entry.get('approval_status')})."
        )
    registry_url = entry.get("url")
    if registry_url and url and registry_url != url:
        raise ControlError("--url이 LiteLLM 레지스트리의 서버 URL과 다릅니다.")
    resolved = registry_url or url
    if not resolved:
        raise ControlError(
            "제어 평면 키에는 서버 URL이 응답되지 않습니다. 계약 값에 쓸 서버 URL을 --url로 "
            "지정하십시오(호출 시점에 레지스트리 값과 다르면 호출이 차단됩니다)."
        )
    transport = entry.get("transport")
    if not transport:
        raise ControlError(f"서버 {server_id}의 전송 방식을 읽을 수 없습니다.")
    if fetchone(
        ctx.conn,
        "SELECT 1 AS x FROM mint.servers WHERE instance_id = %s AND server_id = %s",
        (ctx.instance_id, server_id),
    ):
        raise ControlError(f"서버 {server_id}은(는) 이미 등록되어 있습니다.")
    row_id = new_uuid7()
    auth_type = normalize_auth_type(entry.get("auth_type"))
    digest = url_sha256(resolved)
    row = fetchone(
        ctx.conn,
        "INSERT INTO mint.servers (id, instance_id, server_id, status, url_sha256, transport,"
        " auth_type, registered_by) VALUES (%s, %s, %s, 'awaiting_approval', %s, %s, %s, %s)"
        " RETURNING state_version",
        (row_id, ctx.instance_id, server_id, digest, transport, auth_type, ctx.operator.name),
    )
    ctx.emit(
        EventKind.SERVER_REGISTERED,
        row_id,
        row["state_version"],
        {
            "server_id": server_id,
            "url_sha256": digest,
            "transport": transport,
            "auth_type": auth_type,
            "url_source": "registry" if registry_url else "operator",
        },
    )


def _transition(ctx: Context, server_id: str, source: str, target: str, kind: EventKind) -> None:
    row = fetchone(
        ctx.conn,
        "UPDATE mint.servers SET status = %s WHERE instance_id = %s AND server_id = %s"
        " AND status = %s RETURNING id, state_version",
        (target, ctx.instance_id, server_id, source),
    )
    if row is None:
        raise ControlError(f"{source} 상태의 서버 {server_id}이(가) 없습니다.")
    ctx.emit(kind, row["id"], row["state_version"], {"server_id": server_id, "status": target})


def suspend(ctx: Context, server_id: str) -> None:
    _transition(ctx, server_id, "approved", "suspended", EventKind.SERVER_SUSPENDED)


def resume(ctx: Context, server_id: str) -> None:
    _transition(ctx, server_id, "suspended", "approved", EventKind.SERVER_RESUMED)
