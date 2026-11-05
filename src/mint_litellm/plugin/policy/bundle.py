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


def publish_bundle(path: Path, archive: bytes) -> None:
    path = path.resolve()
    fd, temp_name = tempfile.mkstemp(prefix=".bundle-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(archive)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


async def register_bundle(conn, *, instance_id: str, bundle: PreparedBundle) -> None:
    async with conn.transaction():
        await conn.execute(
            "INSERT INTO mint.policy_versions "
            "(instance_id, id, catalog_sha256, bundle_sha256, status) "
            "VALUES (%s, %s, %s, %s, 'validated')",
            (instance_id, bundle.policy_version_id, bundle.catalog_sha256, bundle.bundle_sha256),
        )


async def _wait_until_loaded(
    client: httpx.AsyncClient, opa_url: str, bundle: "PreparedBundle", budget: float
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
) -> None:
    if timeout_seconds <= 0 or not actor:
        raise ValueError("invalid activation parameters")
    async with asyncio.timeout(timeout_seconds), conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (instance_id,))
        cursor = await conn.execute(
            "SELECT catalog_sha256, bundle_sha256, status FROM mint.policy_versions "
            "WHERE instance_id = %s AND id = %s FOR UPDATE",
            (instance_id, bundle.policy_version_id),
        )
        row = await cursor.fetchone()
        if row != (bundle.catalog_sha256, bundle.bundle_sha256, "validated"):
            raise ValueError("policy version does not match validated bundle")
        await conn.execute(
            "UPDATE mint.policy_versions SET status = 'staged' WHERE instance_id = %s AND id = %s",
            (instance_id, bundle.policy_version_id),
        )
        await publish(bundle.archive)
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            await _wait_until_loaded(
                client, opa_url, bundle, timeout_seconds - min(0.5, timeout_seconds / 5)
            )
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
    )
