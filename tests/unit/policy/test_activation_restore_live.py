import asyncio
import dataclasses
import io
import json
import os
import tarfile
from pathlib import Path

import httpx
import pytest
from test_activation_live import ROOT, _run, _tampered, opa_command

from mint_litellm.plugin.policy.bundle import (
    BundleContentMismatch,
    activate_bundle,
    bundle_content_sha256,
    canonical_json,
    loaded_bundle_sha256,
    parse_data_json,
    prepare_bundle,
    publish_bundle,
    register_bundle,
    restore_bundle,
)

PATH = Path(os.environ.get("MINT_TEST_OPA_BUNDLE_DIR", ".")) / "mint.tar.gz"
NUMBERS = (
    b'"numbers": [1.0, 1e2, 1.50, -0.0, 12345678901234567890123, '
    b"0.1000000000000000055511151231257827, 1E+30],"
)


def _opa_url() -> str:
    return os.environ["MINT_TEST_OPA_URL"].rstrip("/")


def _bundles(*names):
    return [
        prepare_bundle(ROOT, name, opa=opa_command(), opa_policy_path="/work/policy")
        for name in names
    ]


def _mismatching(bundle):
    """메타는 그대로 두고 내용만 바꾼 번들(활성화가 내용 불일치로 거절해야 한다)."""
    return dataclasses.replace(bundle, archive=_tampered(
        bundle.archive, "mint/data/data.json", lambda text: text.replace(b"09:00", b"00:00")
    ))


async def _publish(data):
    publish_bundle(PATH, data)


async def _restore():
    return restore_bundle(PATH)


async def _served(opa_url):
    async with httpx.AsyncClient(trust_env=False) as client:
        meta = (await client.get(f"{opa_url}/v1/data/mint/meta")).json()["result"]
        decision = (await client.post(f"{opa_url}/v1/data/mint/decision", json={"input": {}}))
    return meta, await loaded_bundle_sha256(opa_url), decision.json()["result"]


async def _activate(conn, bundle, publish=_publish, seconds=30, **extra):
    await activate_bundle(
        conn, instance_id="test", actor="operator", bundle=bundle,
        publish=publish, opa_url=_opa_url(), timeout_seconds=seconds, **extra,
    )


@pytest.mark.parametrize("failure", ["content-mismatch", "never-loaded"])
def test_failed_activation_restores_the_previous_bundle(live_database, failure):
    import psycopg

    good, other = _bundles("good", "other")
    bad = _mismatching(other)

    async def stale(_data):
        await _publish(good.archive)

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await register_bundle(conn, instance_id="test", bundle=good)
            await _activate(conn, good, restore=_restore)
            await register_bundle(conn, instance_id="test", bundle=bad)
            await conn.commit()
            expected, publish = (
                (BundleContentMismatch, _publish) if failure == "content-mismatch"
                else (TimeoutError, stale)
            )
            with pytest.raises(expected) as raised:
                await _activate(conn, bad, publish, 10, restore=_restore)
            assert raised.value.restore_outcome == "restored"
            assert "previous bundle: restored" in raised.value.__notes__
            await conn.rollback()
            rows = await (await conn.execute(
                "SELECT id, status FROM mint.policy_versions ORDER BY id"
            )).fetchall()
            assert rows == [("good", "active"), ("other", "validated")]
            meta, content, decision = await _served(_opa_url())
            assert meta == {"policy_version_id": "good", "bundle_sha256": good.bundle_sha256}
            assert content == good.bundle_sha256
            assert (decision["policy_version_id"], decision["bundle_sha256"]) == (
                "good", good.bundle_sha256
            )
            assert PATH.read_bytes() == good.archive

    _run(run)


def test_failed_first_activation_has_nothing_to_restore(live_database):
    import psycopg

    (other,) = _bundles("other")
    bad = _mismatching(other)
    called = []

    async def restore():
        called.append(True)
        return restore_bundle(PATH)

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await register_bundle(conn, instance_id="test", bundle=bad)
            await conn.commit()
            with pytest.raises(BundleContentMismatch) as raised:
                await _activate(conn, bad, seconds=8, restore=restore)
            assert raised.value.restore_outcome == "not restored: no active version"
            assert not called
            await conn.rollback()
            rows = await (await conn.execute(
                "SELECT id, status FROM mint.policy_versions"
            )).fetchall()
            assert rows == [("other", "validated")]

    _run(run)


def test_restore_failures_are_reported_in_the_error(live_database):
    import psycopg

    good, other = _bundles("good", "other")
    bad = _mismatching(other)

    async def no_copy():
        return False

    async def broken():
        raise OSError("disk full")

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await register_bundle(conn, instance_id="test", bundle=good)
            await _activate(conn, good)
            await register_bundle(conn, instance_id="test", bundle=bad)
            await conn.commit()
            for restore, outcome in (
                (no_copy, "not restored: no previous bundle file"),
                (broken, "restore failed: OSError: disk full"),
            ):
                with pytest.raises(BundleContentMismatch) as raised:
                    await _activate(conn, bad, seconds=6, restore=restore)
                assert raised.value.restore_outcome == outcome
                await conn.rollback()
            await _publish(good.archive)
            for _ in range(100):
                if (await _served(_opa_url()))[0]["policy_version_id"] == "good":
                    break
                await asyncio.sleep(0.1)

    _run(run)


def test_number_notation_round_trips_through_opa(live_database):
    import psycopg

    (base,) = _bundles("numbers")
    with tarfile.open(fileobj=io.BytesIO(base.archive), mode="r:gz") as reader:
        members = {m.name: reader.extractfile(m).read() for m in reader.getmembers()}
    data_raw = members["mint/data/data.json"].replace(b"{", b"{" + NUMBERS, 1)
    modules = {k: v for k, v in members.items() if k.endswith(".rego")}
    sha = bundle_content_sha256(modules, {"mint/data/data.json": parse_data_json(data_raw)})
    members["mint/data/data.json"] = data_raw
    members["mint/meta/data.json"] = json.dumps(
        {"policy_version_id": "numbers", "bundle_sha256": sha}
    ).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as writer:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 0
            writer.addfile(info, io.BytesIO(content))
    bundle = dataclasses.replace(base, bundle_sha256=sha, archive=output.getvalue())

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            await register_bundle(conn, instance_id="test", bundle=bundle)
            await _activate(conn, bundle)
            async with httpx.AsyncClient(trust_env=False) as client:
                reply = await client.get(f"{_opa_url()}/v1/data/mint/data")
            served = parse_data_json(reply.text)["result"]
            assert canonical_json(served) == canonical_json(parse_data_json(data_raw))
            assert [str(n) for n in served["numbers"]] == [
                "1.0", "1e2", "1.50", "-0.0", "12345678901234567890123",
                "0.1000000000000000055511151231257827", "1E+30",
            ]
            assert await loaded_bundle_sha256(_opa_url()) == sha

    _run(run)
