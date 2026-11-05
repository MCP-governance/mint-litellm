"""정책 판본 활성화. 번들 준비·게시·정책 엔진 일치 확인·활성 전환은 정책 모듈이 수행한다."""

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
import psycopg

from mint_litellm.control.db.connection import fetchone
from mint_litellm.control.errors import ControlError
from mint_litellm.plugin.policy import bundle as policy_bundle


@dataclass(frozen=True)
class PolicySource:
    root: Path
    opa: str
    bundle_path: Path
    opa_url: str
    timeout_seconds: float


def activate(
    conn: psycopg.Connection,
    database_url: str,
    instance_id: str,
    actor: str,
    version_id: str,
    source: PolicySource,
) -> None:
    """``version_id``로 번들을 준비해 등록하고 활성화한다.

    등록만 되고 전환에 실패한 ``validated`` 판본은 같은 명령으로 다시 게시·활성화한다.
    """
    row = fetchone(
        conn,
        "SELECT status FROM mint.policy_versions WHERE instance_id = %s AND id = %s",
        (instance_id, version_id),
    )
    if row and row["status"] != "validated":
        raise ControlError(
            f"판본 {version_id}은(는) {row['status']} 상태입니다. "
            "새 판본 이름으로 다시 실행하십시오."
        )
    try:
        bundle = policy_bundle.prepare_bundle(source.root, version_id, opa=source.opa)
    except FileNotFoundError as error:
        raise ControlError(f"정책 번들을 준비할 수 없습니다: {error}") from None
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or b"").decode("utf-8", "replace").strip()
        raise ControlError(f"정책 시험이 실패했습니다: {detail}") from None
    except (ValueError, OSError, KeyError) as error:
        raise ControlError(f"정책 번들이 올바르지 않습니다: {error}") from None

    async def publish(archive: bytes) -> None:
        policy_bundle.publish_bundle(source.bundle_path, archive)

    async def keep(active_version: str, active_sha: str) -> None:
        policy_bundle.keep_active_bundle(source.bundle_path, active_version, active_sha)

    async def restore(active_version: str, active_sha: str) -> bool:
        return policy_bundle.restore_bundle(source.bundle_path, active_version, active_sha)

    async def connect() -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(database_url)

    async def run() -> None:
        async with await connect() as aconn:
            if row is None:
                await policy_bundle.register_bundle(
                    aconn, instance_id=instance_id, bundle=bundle
                )
            activate_fn = (
                policy_bundle.retry_activation if row else policy_bundle.activate_bundle
            )
            await activate_fn(
                aconn,
                instance_id=instance_id,
                actor=actor,
                bundle=bundle,
                publish=publish,
                opa_url=source.opa_url,
                timeout_seconds=source.timeout_seconds,
                keep=keep,
                restore=restore,
                connect=connect,
            )

    try:
        asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop)
    except TimeoutError as error:
        raise ActivationError(
            "정책 엔진이 시간 안에 새 번들을 적재하지 않았습니다. 판본은 활성화되지 않았으며 "
            "같은 명령으로 다시 시도할 수 있습니다.",
            restore_outcome(error),
        ) from None
    except (
        asyncio.CancelledError, ValueError, OSError, httpx.HTTPError, psycopg.Error
    ) as error:
        message = f"정책 활성화에 실패했습니다: {error}"
        raise ActivationError(message, restore_outcome(error)) from None


class ActivationError(ControlError):
    """활성화 실패. ``restore_outcome``은 직전 번들을 되돌린 결과(알 수 없으면 None)이다."""

    def __init__(self, message: str, outcome: str | None) -> None:
        suffix = f" 직전 번들: {outcome}." if outcome else ""
        super().__init__(message + suffix)
        self.restore_outcome = outcome


def restore_outcome(error: BaseException | None) -> str | None:
    """복원 결과. 취소는 ``CancelledError.restore_outcome``, 바깥 제한은 ``__cause__``에 있다."""
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        outcome = getattr(error, "restore_outcome", None)
        if outcome:
            return str(outcome)
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return None
