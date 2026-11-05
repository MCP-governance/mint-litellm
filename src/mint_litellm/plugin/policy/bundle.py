"""정책 번들 준비와 활성 판본 전환."""

import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from mint_litellm.types.events import EventKind, control_source_key
from mint_litellm.types.ids import new_uuid7

DATA_FILE = "mint/data/data.json"
META_FILE = "mint/meta/data.json"


class BundleContentMismatch(ValueError):
    """OPA가 적재한 번들 내용이 등록된 해시와 다르다."""


def _reject_constant(name: str) -> None:
    raise ValueError(f"invalid JSON constant {name}")


class _Number(str):
    """JSON 숫자의 원문 표기. 반올림이나 변환 없이 그대로 해시에 들어간다."""


def parse_data_json(raw: str | bytes) -> object:
    return json.loads(
        raw, parse_int=_Number, parse_float=_Number, parse_constant=_reject_constant
    )


def _canonical(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, _Number):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + _canonical(value[key])
            for key in sorted(value)
        ) + "}"
    raise ValueError("unsupported JSON value")


def canonical_json(value: object) -> bytes:
    """키 정렬, 구분자 ``,``·``:``, UTF-8 정규 JSON."""
    return _canonical(value).encode("utf-8")


def bundle_content_sha256(
    modules: Mapping[str, str | bytes], data: Mapping[str, object]
) -> str:
    """번들 내용 해시. 경로는 번들 안 상대 경로, 규칙은 원문, 데이터는 정규 JSON이다.

    번들 준비와 OPA 적재 내용의 재계산이 같은 함수를 쓴다.
    """
    entries = {
        **{path: raw.encode("utf-8") if isinstance(raw, str) else raw
           for path, raw in modules.items()},
        **{path: canonical_json(value) for path, value in data.items()},
    }
    if len(entries) != len(modules) + len(data):
        raise ValueError("duplicate bundle path")
    digest = hashlib.sha256()
    for path in sorted(entries):
        digest.update(path.encode("utf-8") + b"\0" + entries[path] + b"\0")
    return digest.hexdigest()


async def loaded_bundle_sha256(opa_url: str, client: httpx.AsyncClient | None = None) -> str:
    """OPA가 실제로 적재한 모듈 원문과 정책 데이터에서 번들 내용 해시를 다시 계산한다."""
    base = opa_url.rstrip("/")
    if client is None:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as own:
            return await loaded_bundle_sha256(opa_url, own)

    async def get(path: str) -> dict:
        response = await client.get(base + path)
        response.raise_for_status()
        body = parse_data_json(response.text)
        return body if isinstance(body, dict) else {}

    bundles = (await get("/v1/data/system/bundles")).get("result")
    if not isinstance(bundles, dict) or len(bundles) != 1:
        raise BundleContentMismatch("exactly one loaded bundle is required")
    prefix = next(iter(bundles)).rstrip("/") + "/"
    modules: dict[str, str] = {}
    policies = (await get("/v1/policies")).get("result")
    for policy in policies if isinstance(policies, list) else []:
        identifier, raw = policy.get("id"), policy.get("raw")
        if not (isinstance(identifier, str) and isinstance(raw, str)
                and identifier.startswith(prefix)):
            raise BundleContentMismatch("module outside the loaded bundle")
        modules[identifier.removeprefix(prefix)] = raw
    tree = (await get("/v1/data")).get("result")
    unexpected = _unexpected_data(tree, _rule_paths(policies))
    if unexpected:
        raise BundleContentMismatch(f"data outside the bundle contract at {unexpected}")
    if not _system_is_managed(bundles, (await get("/v1/data/system")).get("result")):
        raise BundleContentMismatch("data under system is not managed by OPA")
    data = {}
    document = (await get("/v1/data/mint/data")).get("result")
    if document is not None:
        data[DATA_FILE] = document
    return bundle_content_sha256(modules, data)


def _text(value: object) -> bool:
    return type(value) is str


def _manifest_is_managed(manifest: object) -> bool:
    if not isinstance(manifest, dict):
        return False
    shapes = {
        "revision": _text,
        "roots": lambda v: isinstance(v, list) and all(_text(item) for item in v),
        "metadata": lambda v: isinstance(v, dict),
        "rego_version": lambda v: isinstance(v, _Number),
        "file_rego_versions": lambda v: isinstance(v, dict)
        and all(isinstance(item, _Number) for item in v.values()),
        "wasm": lambda v: isinstance(v, list),
    }
    return all(key in shapes and shapes[key](value) for key, value in manifest.items())


def _system_is_managed(bundles: dict, system: object) -> bool:
    """``data.system``에 OPA가 쓰는 항목만, 정해진 키와 형식으로 있는지 본다.

    지원하는 적재 경로는 파일 번들과 ``--watch``(OPA가 판본 정보를 다시 쓴다)이다.
    전체 조회는 이 경로를 빼고 돌려주므로 따로 조회한다.
    """
    if not isinstance(system, dict) or set(system) != {"bundles", "version"}:
        return False
    version = system["version"]
    if system["bundles"] != bundles or not (
        isinstance(version, dict)
        and set(version) == {"build_commit", "build_hostname", "build_timestamp", "version"}
        and all(_text(value) for value in version.values())
    ):
        return False
    return all(
        isinstance(entry, dict)
        and set(entry) == {"etag", "manifest"}
        and _text(entry["etag"])
        and _manifest_is_managed(entry["manifest"])
        for entry in bundles.values()
    )


def _rule_paths(policies: object) -> set[tuple[str, ...]]:
    """적재된 모듈의 규칙이 `data` 아래에서 차지하는 경로(패키지 경로 + 규칙 머리)."""
    paths: set[tuple[str, ...]] = set()
    for policy in policies if isinstance(policies, list) else []:
        ast = policy.get("ast") or {}
        package = tuple(term["value"] for term in ast["package"]["path"][1:])
        for rule in ast.get("rules", []):
            names: list[str] = []
            for term in rule["head"]["ref"]:
                if term["type"] not in {"var", "string"} or (names and term["type"] == "var"):
                    break
                names.append(term["value"])
            paths.add(package + tuple(names))
    return paths


def _unexpected_data(
    tree: object, rule_paths: set[tuple[str, ...]], path: tuple[str, ...] = ()
) -> list[str]:
    """규칙이 만든 문서와 허용된 데이터 문서(`mint/data`, `mint/meta`)가 아닌 항목의 경로."""
    if path in rule_paths or path == ("mint", "data"):
        return []
    if path == ("mint", "meta"):
        keys = {"policy_version_id", "bundle_sha256"}
        return [] if isinstance(tree, dict) and set(tree) == keys else ["/".join(path)]
    if not isinstance(tree, dict) or not any(p[: len(path)] == path for p in rule_paths):
        return ["/".join(path) or "data"]
    found: list[str] = []
    for key, value in tree.items():
        found += _unexpected_data(value, rule_paths, (*path, key))
    return found


@dataclass(frozen=True)
class PreparedBundle:
    policy_version_id: str
    bundle_sha256: str
    catalog_sha256: str
    archive: bytes


def _validate_hours(hours: object) -> None:
    if not isinstance(hours, dict) or set(hours) != {"tz", "days", "start", "end"}:
        raise ValueError("invalid business hours")
    try:
        ZoneInfo(hours["tz"])
    except (KeyError, TypeError, ZoneInfoNotFoundError) as exc:
        raise ValueError("invalid timezone") from exc
    days = hours["days"]
    if not isinstance(days, list) or not days or any(
        type(d) is not int or d not in range(1, 8) for d in days
    ):
        raise ValueError("invalid business days")
    for value in (hours["start"], hours["end"]):
        if not isinstance(value, str) or not re.fullmatch(
            r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value
        ):
            raise ValueError("invalid business time")
    if hours["start"] >= hours["end"]:
        raise ValueError("business start must precede end")


def prepare_bundle(
    root: Path,
    policy_version_id: str,
    *,
    opa: str | Sequence[str] = "opa",
    opa_policy_path: str | None = None,
) -> PreparedBundle:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", policy_version_id):
        raise ValueError("invalid policy version")
    root = root.resolve()
    bundle_dir = root / "policy" / "bundle"
    files = sorted(
        (p for p in bundle_dir.rglob("*") if p.is_file() and p.suffix in {".rego", ".json"}
         and p.relative_to(bundle_dir).as_posix() != META_FILE),
        key=lambda p: p.relative_to(bundle_dir).as_posix(),
    )
    if not files or any(
        p.is_symlink() or not p.resolve().is_relative_to(bundle_dir) for p in files
    ):
        raise ValueError("invalid bundle files")
    contents = [(p.relative_to(bundle_dir).as_posix(), p.read_bytes()) for p in files]
    source = dict(contents)
    if {name for name in source if name.endswith(".json")} != {DATA_FILE}:
        raise ValueError("invalid bundle data files")
    data = parse_data_json(source[DATA_FILE])
    _validate_hours(json.loads(source[DATA_FILE])["business_hours"])
    catalog = (root / "policy" / "catalog.json").read_bytes()
    catalog_data = json.loads(catalog)
    if {rule["id"] for rule in catalog_data["rules"]} != {"R1", "R2", "R3", "R4"}:
        raise ValueError("invalid catalog")
    command = [opa] if isinstance(opa, str) else list(opa)
    subprocess.run(
        [*command, "test", opa_policy_path or str(root / "policy")],
        check=True,
        capture_output=True,
    )
    if any((bundle_dir / name).read_bytes() != content for name, content in contents):
        raise ValueError("bundle changed during validation")
    if (root / "policy" / "catalog.json").read_bytes() != catalog:
        raise ValueError("catalog changed during validation")
    sha = bundle_content_sha256(
        {name: content for name, content in contents if name != DATA_FILE}, {DATA_FILE: data}
    )
    meta = json.dumps({"policy_version_id": policy_version_id, "bundle_sha256": sha}).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, content in [*contents, (META_FILE, meta)]:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return PreparedBundle(
        policy_version_id, sha, hashlib.sha256(catalog).hexdigest(), stream.getvalue()
    )


def _write_atomic(path: Path, data: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".bundle-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _previous_path(path: Path) -> Path:
    return path.with_name(path.name + ".previous")


def _archive_identity(data: bytes) -> tuple[str, str] | None:
    """아카이브 메타의 (판본 ID, 내용 해시). 메타가 실제 내용과 다르거나 읽을 수 없으면 None."""
    try:
        modules: dict[str, bytes] = {}
        documents: dict[str, object] = {}
        meta = None
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive.getmembers():
                body = archive.extractfile(member).read()
                if member.name == META_FILE:
                    meta = json.loads(body)
                elif member.name.endswith(".json"):
                    documents[member.name] = parse_data_json(body)
                else:
                    modules[member.name] = body
        identity = (meta["policy_version_id"], meta["bundle_sha256"])
        if bundle_content_sha256(modules, documents) != identity[1]:
            return None
    except (tarfile.TarError, OSError, KeyError, ValueError, TypeError, AttributeError):
        return None
    return identity if all(isinstance(part, str) for part in identity) else None


def publish_bundle(path: Path, archive: bytes) -> None:
    """번들을 원자적으로 게시한다."""
    _write_atomic(path.resolve(), archive)


def keep_active_bundle(path: Path, version_id: str, active_sha: str) -> None:
    """게시 전에 현재 파일이 활성 판본의 온전한 번들이면 `<이름>.previous`로 보관한다.

    현재 파일이 활성 판본이 아니면(실패한 게시가 남은 경우 등) 기존 보관본을 건드리지 않는다.
    """
    path = path.resolve()
    if path.is_file():
        data = path.read_bytes()
        if _archive_identity(data) == (version_id, active_sha):
            _write_atomic(_previous_path(path), data)


def restore_bundle(path: Path, version_id: str, active_sha: str) -> bool:
    """활성 판본과 판본 ID·내용이 같은 온전한 보관본이 있을 때만 원자적으로 되돌린다."""
    path = path.resolve()
    previous = _previous_path(path)
    if not previous.is_file():
        return False
    data = previous.read_bytes()
    if _archive_identity(data) != (version_id, active_sha):
        return False
    _write_atomic(path, data)
    return True


async def register_bundle(conn, *, instance_id: str, bundle: PreparedBundle) -> None:
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO mint.policy_versions "
            "(instance_id, id, catalog_sha256, bundle_sha256, status) "
            "VALUES (%s, %s, %s, %s, 'validated')",
            (instance_id, bundle.policy_version_id, bundle.catalog_sha256, bundle.bundle_sha256),
        )


@dataclass(frozen=True)
class _Served:
    policy_version_id: str
    bundle_sha256: str


RESTORED = "restored"
NOT_RESTORED_NO_ACTIVE = "not restored: no active version"
NOT_RESTORED_NO_COPY = "not restored: no verified copy of the active bundle"
NOT_RESTORED_COMMITTED = "not restored: activation was committed"
NOT_RESTORED_UNKNOWN = "not restored: connection lost, commit result unknown"
RESTORE_INTERRUPTED = "restore interrupted"


def _attach(exc: BaseException, outcome: str) -> None:
    exc.restore_outcome = outcome
    exc.add_note(f"previous bundle: {outcome}")


async def _recover(
    conn,
    instance_id: str,
    bundle: "PreparedBundle",
    restore: Callable[[str, str], Awaitable[bool]],
    opa_url: str,
    deadline: float,
) -> str:
    """실패 뒤 잠금 안에서 DB의 활성 판본을 다시 읽고, 그 판본의 번들로 되돌려 확인한다.

    커밋 결과가 불명인 경우에도 DB가 새 판본을 활성으로 보이면 되돌리지 않는다.
    """
    loop = asyncio.get_running_loop()
    try:
        if conn.closed or conn.broken:
            return NOT_RESTORED_UNKNOWN
        async with asyncio.timeout_at(deadline), conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (instance_id,)
            )
            cursor = await conn.execute(
                "SELECT id, bundle_sha256 FROM mint.policy_versions "
                "WHERE instance_id = %s AND status = 'active'", (instance_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                return NOT_RESTORED_NO_ACTIVE
            active = _Served(*row)
            if (active.policy_version_id, active.bundle_sha256) == (
                bundle.policy_version_id, bundle.bundle_sha256
            ):
                return NOT_RESTORED_COMMITTED
            if not await restore(active.policy_version_id, active.bundle_sha256):
                return NOT_RESTORED_NO_COPY
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
                await _wait_until_loaded(
                    client, opa_url, active, max(deadline - loop.time(), 0.1)
                )
            return RESTORED
    except Exception as exc:
        return f"restore failed: {type(exc).__name__}: {exc}"


async def _wait_until_loaded(
    client: httpx.AsyncClient, opa_url: str, bundle: "PreparedBundle | _Served", budget: float
) -> None:
    """남은 예산 안에서 메타와 적재 내용이 함께 등록 값과 같아질 때까지 다시 확인한다."""
    expected = {
        "policy_version_id": bundle.policy_version_id,
        "bundle_sha256": bundle.bundle_sha256,
    }
    mismatch: BundleContentMismatch | None = None
    try:
        async with asyncio.timeout(budget):
            while True:
                response = await client.get(opa_url.rstrip("/") + "/v1/data/mint/meta")
                response.raise_for_status()
                if response.json().get("result") == expected:
                    try:
                        if await loaded_bundle_sha256(opa_url, client) == bundle.bundle_sha256:
                            return
                        mismatch = BundleContentMismatch(
                            "loaded bundle content differs from registered hash"
                        )
                    except BundleContentMismatch as exc:
                        mismatch = exc
                await asyncio.sleep(0.1)
    except TimeoutError:
        if mismatch is not None:
            raise mismatch from None
        raise


async def activate_bundle(
    conn,
    *,
    instance_id: str,
    actor: str,
    bundle: PreparedBundle,
    publish: Callable[[bytes], Awaitable[None]],
    opa_url: str,
    timeout_seconds: float,
    keep: Callable[[str, str], Awaitable[None]] | None = None,
    restore: Callable[[str, str], Awaitable[bool]] | None = None,
) -> None:
    """번들을 게시하고 활성화한다.

    ``keep(판본 ID, 해시)``는 게시 직전에, ``restore(판본 ID, 해시)``는 실패 뒤에 호출된다.
    ``restore``를 주면 게시를 시작한 뒤의 실패(적재 불일치·시간 초과·취소·DB 확정 실패)에서
    잠금 안에서 DB의 활성 판본을 다시 읽어 그 번들로 되돌리고 OPA가 다시 서빙하는지 확인한다.
    결과는 던져지는 예외(취소 포함)의 ``restore_outcome``과 노트에 담긴다. 활성 판본과 내용이
    같은 보관본이 없거나, 연결이 끊겨 커밋 결과를 알 수 없거나, DB가 이미 새 판본을 활성으로
    보이면 되돌리지 않는다. 복원 시간은 전체 제한(``timeout_seconds``) 안에서 먼저 떼어 둔다.
    """
    if timeout_seconds <= 0 or not actor:
        raise ValueError("invalid activation parameters")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    tail = min(0.5, timeout_seconds / 5)
    reserve = max(tail, timeout_seconds * 0.3) if restore else tail
    published = False
    try:
        async with conn.transaction():
            async with asyncio.timeout_at(deadline):
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (instance_id,)
                )
                cursor = await conn.execute(
                    "SELECT catalog_sha256, bundle_sha256, status FROM mint.policy_versions "
                    "WHERE instance_id = %s AND id = %s FOR UPDATE",
                    (instance_id, bundle.policy_version_id),
                )
                row = await cursor.fetchone()
                if row != (bundle.catalog_sha256, bundle.bundle_sha256, "validated"):
                    raise ValueError("policy version does not match validated bundle")
                await conn.execute(
                    "UPDATE mint.policy_versions SET status = 'staged' "
                    "WHERE instance_id = %s AND id = %s",
                    (instance_id, bundle.policy_version_id),
                )
                cursor = await conn.execute(
                    "SELECT id, bundle_sha256 FROM mint.policy_versions "
                    "WHERE instance_id = %s AND status = 'active'", (instance_id,)
                )
                active = await cursor.fetchone()
                if keep is not None and active is not None:
                    await keep(*active)
            published = True
            async with asyncio.timeout_at(deadline - reserve):
                await publish(bundle.archive)
                async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
                    await _wait_until_loaded(
                        client, opa_url, bundle, max(deadline - reserve - loop.time() - 0.1, 0.001)
                    )
            await _switch_active(conn, instance_id, actor, bundle)
    except BaseException as exc:
        if restore is not None and published:
            try:
                outcome = await _recover(
                    conn, instance_id, bundle, restore, opa_url,
                    max(deadline, loop.time() + 1.0),
                )
            except asyncio.CancelledError as cancelled:
                _attach(cancelled, RESTORE_INTERRUPTED)
                _attach(exc, RESTORE_INTERRUPTED)
                raise
            _attach(exc, outcome)
        raise


async def _switch_active(conn, instance_id: str, actor: str, bundle: PreparedBundle) -> None:
    await conn.execute(
        "UPDATE mint.policy_versions SET status = 'superseded' "
        "WHERE instance_id = %s AND status = 'active'", (instance_id,)
    )
    cursor = await conn.execute(
        "UPDATE mint.policy_versions SET status = 'active', activated_at = now(), "
        "activated_by = %s "
        "WHERE instance_id = %s AND id = %s AND status = 'staged' RETURNING state_version",
        (actor, instance_id, bundle.policy_version_id),
    )
    version_row = await cursor.fetchone()
    if version_row is None:
        raise ValueError("policy activation failed")
    source_key = control_source_key(
        EventKind.POLICY_ACTIVATED, bundle.policy_version_id, version_row[0]
    )
    payload = json.dumps(
        {"policy_version_id": bundle.policy_version_id, "bundle_sha256": bundle.bundle_sha256}
    )
    await conn.execute(
        "INSERT INTO mint.events (event_id, instance_id, kind, source_key, actor, payload) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
        (
            new_uuid7(), instance_id, EventKind.POLICY_ACTIVATED.value,
            source_key, actor, payload,
        ),
    )


async def retry_activation(
    conn,
    *,
    instance_id: str,
    actor: str,
    bundle: PreparedBundle,
    publish: Callable[[bytes], Awaitable[None]],
    opa_url: str,
    timeout_seconds: float,
    keep: Callable[[str, str], Awaitable[None]] | None = None,
    restore: Callable[[str, str], Awaitable[bool]] | None = None,
) -> None:
    """게시 후 DB 전환이 실패한 validated 판본을 다시 게시하고 활성화한다."""
    await activate_bundle(
        conn,
        instance_id=instance_id,
        actor=actor,
        bundle=bundle,
        publish=publish,
        opa_url=opa_url,
        timeout_seconds=timeout_seconds,
        keep=keep,
        restore=restore,
    )
