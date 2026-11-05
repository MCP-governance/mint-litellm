"""활성 정책 판본의 내용 대조. 정책 엔진이 적재한 번들의 해시를 다시 계산해 등록값과 비교한다."""

import asyncio
from dataclasses import dataclass

import httpx
import psycopg

from mint_litellm.control.db.connection import fetchone
from mint_litellm.plugin.policy.bundle import BundleContentMismatch, loaded_bundle_sha256


@dataclass(frozen=True)
class PolicyState:
    """``state``는 ``none``(활성 판본 없음), ``match``, ``mismatch``, ``unverified``(확인 못함)."""

    state: str
    version: str | None = None
    expected: str | None = None
    loaded: str | None = None
    detail: str = ""


async def _read_loaded(opa_url: str) -> tuple[object, str]:
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        response = await client.get(opa_url.rstrip("/") + "/v1/data/mint/meta")
        response.raise_for_status()
        meta = response.json().get("result")
        return meta, await loaded_bundle_sha256(opa_url, client)


def verify_active_policy(
    conn: psycopg.Connection, instance_id: str, opa_url: str | None
) -> PolicyState:
    row = fetchone(
        conn,
        "SELECT id, bundle_sha256 FROM mint.policy_versions"
        " WHERE instance_id = %s AND status = 'active'",
        (instance_id,),
    )
    if row is None:
        return PolicyState("none", detail="활성 정책 판본이 없습니다.")
    version, expected = row["id"], row["bundle_sha256"]
    if not opa_url:
        return PolicyState("unverified", version, expected,
                           detail="정책 엔진 주소(MINT_OPA_URL)가 없어 대조하지 못했습니다.")
    try:
        meta, loaded = asyncio.run(_read_loaded(opa_url), loop_factory=asyncio.SelectorEventLoop)
    except BundleContentMismatch as error:
        return PolicyState(
            "mismatch", version, expected, detail=f"적재된 번들이 올바르지 않습니다: {error}"
        )
    except (httpx.HTTPError, OSError, ValueError) as error:
        return PolicyState("unverified", version, expected,
                           detail=f"정책 엔진에서 적재 내용을 읽지 못했습니다: {error}")
    if meta != {"policy_version_id": version, "bundle_sha256": expected}:
        return PolicyState(
            "mismatch", version, expected, loaded,
            f"정책 엔진의 메타(판본·해시)가 활성 판본 {version}과 다릅니다.",
        )
    if loaded != expected:
        return PolicyState(
            "mismatch", version, expected, loaded,
            f"판본 {version}의 등록 해시와 정책 엔진에 적재된 내용의 해시가 다릅니다.",
        )
    return PolicyState("match", version, expected, loaded)
