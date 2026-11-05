"""동의. 1단계에서는 운영자가 외부 증거를 붙여 증명한 동의만 등록한다."""

import re
from datetime import datetime

from mint_litellm.control import orgs
from mint_litellm.control.context import Context
from mint_litellm.control.db.connection import fetchone
from mint_litellm.control.errors import ControlError
from mint_litellm.control.litellm_admin import LiteLLMAdmin
from mint_litellm.control.operators import HUMAN_ROLES
from mint_litellm.types.events import EventKind
from mint_litellm.types.ids import new_uuid7

_SHA256 = re.compile(r"[0-9a-f]{64}")


def add(
    ctx: Context,
    litellm: LiteLLMAdmin,
    org_id: str,
    human_user_id: str,
    server_id: str,
    purpose: str,
    data_scope: str,
    text_version: str,
    granted_at: datetime,
    valid_until: datetime,
    evidence_ref: str,
    evidence_sha256: str,
) -> str:
    if not evidence_ref.strip():
        raise ControlError("증거 참조(--evidence-ref)가 필요합니다.")
    if not _SHA256.fullmatch(evidence_sha256):
        raise ControlError("증거 해시(--evidence-sha256)는 소문자 16진수 64자여야 합니다.")
    if granted_at >= valid_until:
        raise ControlError("valid-until은 granted-at보다 뒤여야 합니다.")
    orgs.require_active(ctx, org_id)
    user = litellm.get_user(human_user_id)
    if user is None or user.get("user_role") not in HUMAN_ROLES:
        raise ControlError(f"LiteLLM에 사람 사용자 {human_user_id}가 없습니다.")
    if not fetchone(
        ctx.conn,
        "SELECT 1 AS x FROM mint.servers WHERE instance_id = %s AND server_id = %s",
        (ctx.instance_id, server_id),
    ):
        raise ControlError(f"등록되지 않은 서버입니다: {server_id}")
    consent_id = new_uuid7()
    row = fetchone(
        ctx.conn,
        "INSERT INTO mint.consents (id, instance_id, org_id, human_user_id, server_id, purpose,"
        " data_scope, text_version, source, evidence_ref, evidence_sha256, granted_at,"
        " valid_until, registered_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,"
        " 'operator_attested', %s, %s, %s, %s, %s) RETURNING state_version",
        (consent_id, ctx.instance_id, org_id, human_user_id, server_id, purpose, data_scope,
         text_version, evidence_ref, evidence_sha256, granted_at, valid_until, ctx.operator.name),
    )
    ctx.emit(
        EventKind.CONSENT_GRANTED,
        consent_id,
        row["state_version"],
        {
            "consent_id": str(consent_id),
            "org_id": org_id,
            "human_user_id": human_user_id,
            "server_id": server_id,
            "purpose": purpose,
            "source": "operator_attested",
            "evidence_ref": evidence_ref,
            "evidence_sha256": evidence_sha256,
            "valid_until": valid_until.isoformat(),
        },
    )
    return str(consent_id)


def revoke(ctx: Context, consent_id: str) -> None:
    row = fetchone(
        ctx.conn,
        "UPDATE mint.consents SET revoked_at = now() WHERE instance_id = %s AND id = %s"
        " AND revoked_at IS NULL RETURNING state_version, human_user_id, server_id, purpose",
        (ctx.instance_id, consent_id),
    )
    if row is None:
        raise ControlError(f"철회되지 않은 동의 {consent_id}이(가) 없습니다.")
    ctx.emit(
        EventKind.CONSENT_REVOKED,
        consent_id,
        row["state_version"],
        {
            "consent_id": consent_id,
            "human_user_id": row["human_user_id"],
            "server_id": row["server_id"],
            "purpose": row["purpose"],
        },
    )
