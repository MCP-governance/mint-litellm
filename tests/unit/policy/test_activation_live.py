import asyncio
import dataclasses
import io
import os
import shlex
import sys
import tarfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from mint_litellm.plugin.policy.bundle import (
    BundleContentMismatch,
    activate_bundle,
    loaded_bundle_sha256,
    prepare_bundle,
    publish_bundle,
    register_bundle,
    retry_activation,
)

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "src/mint_litellm/control/db/migrations/001_initial.sql"
IMAGE = (
    "openpolicyagent/opa:1.21.4@sha256:"
    "12420358d3f22a0cd2424ddc44a16e292ecb42825d3dfbca779986006822a3f6"
)
REQUIRED_ENV = ("MINT_TEST_DATABASE_URL", "MINT_TEST_OPA_URL", "MINT_TEST_OPA_BUNDLE_DIR")


@pytest.fixture
def live_database() -> Iterator[str]:
    if not all(os.environ.get(key) for key in REQUIRED_ENV):
        pytest.skip("live database, OPA URL and bundle directory are required")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    admin_url = os.environ["MINT_TEST_DATABASE_URL"]
    name = f"mint_policy_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(admin_url)
    params["dbname"] = name
    url = make_conninfo(**params)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def opa_command() -> list[str]:
    docker = shlex.split(os.environ.get("MINT_TEST_DOCKER", "docker"))
    mount = os.environ.get("MINT_TEST_DOCKER_SOURCE", str(ROOT.resolve()))
    return [*docker, "run", "--rm", "-v", f"{mount}:/work:ro", IMAGE]


def test_real_activation_and_recovery(live_database):
    import psycopg

    bundle_dir = Path(os.environ["MINT_TEST_OPA_BUNDLE_DIR"])
    assert bundle_dir.is_dir()
    opa_url = os.environ["MINT_TEST_OPA_URL"].rstrip("/")
    first, second, third = (
        prepare_bundle(ROOT, name, opa=opa_command(), opa_policy_path="/work/policy")
        for name in ("first", "second", "third")
    )

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            async def publish(data):
                publish_bundle(bundle_dir / "mint.tar.gz", data)

            async def activate(bundle):
                await activate_bundle(
                    conn, instance_id="test", actor="operator", bundle=bundle,
                    publish=publish, opa_url=opa_url, timeout_seconds=30,
                )

            async def snapshot():
                rows = await (await conn.execute(
                    "SELECT id, status, state_version FROM mint.policy_versions ORDER BY id"
                )).fetchall()
                epoch = await (await conn.execute(
                    "SELECT epoch FROM mint.state_epoch WHERE instance_id = %s", ("test",)
                )).fetchone()
                events = await (await conn.execute(
                    "SELECT count(*) FROM mint.events WHERE kind = %s", ("policy_activated",)
                )).fetchone()
                await conn.commit()
                return rows, epoch[0], events[0]

            await register_bundle(conn, instance_id="test", bundle=first)
            initial, epoch_initial, _ = await snapshot()
            await activate(first)
            first_rows, first_epoch, first_events = await snapshot()
            assert first_rows == [("first", "active", initial[0][2] + 2)]
            assert first_epoch > epoch_initial and first_events == 1

            await register_bundle(conn, instance_id="test", bundle=second)
            before_second, before_epoch, before_events = await snapshot()
            await activate(second)
            after_second, after_epoch, after_events = await snapshot()
            assert [row[:2] for row in after_second] == [
                ("first", "superseded"), ("second", "active")
            ]
            assert after_second[0][2] > before_second[0][2]
            assert after_second[1][2] == before_second[1][2] + 2
            assert after_epoch > before_epoch and after_events == before_events + 1
            active = await (await conn.execute(
                "SELECT id, bundle_sha256 FROM mint.policy_versions WHERE status = 'active'"
            )).fetchone()
            await conn.commit()
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(f"{opa_url}/v1/data/mint/decision", json={"input": {}})
                response.raise_for_status()
                result = response.json()["result"]
                assert (result["policy_version_id"], result["bundle_sha256"]) == active

            await register_bundle(conn, instance_id="test", bundle=third)
            before_failure = await snapshot()

            async def publish_wrong(_data):
                await publish(second.archive)

            with pytest.raises(TimeoutError):
                await activate_bundle(
                    conn, instance_id="test", actor="operator", bundle=third,
                    publish=publish_wrong, opa_url=opa_url, timeout_seconds=2,
                )
            assert await snapshot() == before_failure

            loaded = asyncio.Event()

            async def publish_then_wait(data):
                await publish(data)
                async with httpx.AsyncClient(trust_env=False) as client:
                    while True:
                        response = await client.get(f"{opa_url}/v1/data/mint/meta")
                        response.raise_for_status()
                        if response.json().get("result", {}).get("policy_version_id") == "third":
                            loaded.set()
                            break
                        await asyncio.sleep(0.1)
                await asyncio.sleep(60)

            with pytest.raises(TimeoutError):
                await activate_bundle(
                    conn, instance_id="test", actor="operator", bundle=third,
                    publish=publish_then_wait, opa_url=opa_url, timeout_seconds=15,
                )
            assert loaded.is_set()
            assert await snapshot() == before_failure
            await retry_activation(
                conn, instance_id="test", actor="operator", bundle=third,
                publish=publish, opa_url=opa_url, timeout_seconds=30,
            )
            final, final_epoch, final_events = await snapshot()
            assert [row[:2] for row in final] == [
                ("first", "superseded"), ("second", "superseded"), ("third", "active")
            ]
            assert final_epoch > before_failure[1] and final_events == before_failure[2] + 1

    if sys.platform == "win32":
        policy = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        try:
            asyncio.run(run())
        finally:
            asyncio.set_event_loop_policy(policy)
    else:
        asyncio.run(run())


def _run(coroutine_function):
    if sys.platform == "win32":
        policy = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        try:
            asyncio.run(coroutine_function())
        finally:
            asyncio.set_event_loop_policy(policy)
    else:
        asyncio.run(coroutine_function())


def _tampered(archive: bytes, name: str, change) -> bytes:
    """메타는 그대로 두고 번들 안 한 파일을 바꾸거나(없으면 ``change(b"")``로 추가한) 아카이브."""
    source = io.BytesIO(archive)
    output = io.BytesIO()
    with tarfile.open(fileobj=source, mode="r:gz") as reader, \
            tarfile.open(fileobj=output, mode="w:gz") as writer:
        members = {m.name: reader.extractfile(m).read() for m in reader.getmembers()}
        members[name] = change(members.get(name, b""))
        for member_name, content in members.items():
            info = tarfile.TarInfo(member_name)
            info.size = len(content)
            info.mtime = 0
            writer.addfile(info, io.BytesIO(content))
    return output.getvalue()


@pytest.mark.parametrize(
    ("name", "change"),
    [
        ("mint/policy.rego", lambda text: text + b"# edited" + bytes([10])),
        ("mint/data/data.json", lambda text: text.replace(b"09:00", b"00:00")),
        ("mint/data/inner/data.json", lambda _: b'{"d": 1}'),
        ("data.json", lambda _: b'{"root_extra": 1}'),
        ("mint/extra/data.json", lambda _: b'{"k": 1}'),
        ("mint/data.json", lambda _: b'{"extra": 2}'),
        ("mint/policy/data.json", lambda _: b'{"extra": 3}'),
        ("mint/decision/data.json", lambda _: b'{"extra": 4}'),
        ("system/data.json", lambda _: b'{"injected": {"k": 1}}'),
        ("system/sub/data.json", lambda _: b'{"k": 1}'),
        ("system/bundles/data.json", lambda _: b'{"x": 1}'),
    ],
    ids=[
        "rule-line", "data-value", "data-subtree", "root-data", "other-data-path",
        "mint-data", "rule-package-data", "decision-package-data", "system-data",
        "system-subtree", "system-bundles",
    ],
)
def test_activation_rejects_content_changed_with_unchanged_metadata(live_database, name, change):
    import psycopg

    bundle_dir = Path(os.environ["MINT_TEST_OPA_BUNDLE_DIR"])
    opa_url = os.environ["MINT_TEST_OPA_URL"].rstrip("/")
    good, tampered = (
        prepare_bundle(ROOT, version, opa=opa_command(), opa_policy_path="/work/policy")
        for version in ("good", "tampered")
    )
    forged = dataclasses.replace(tampered, archive=_tampered(tampered.archive, name, change))

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            async def publish(data):
                publish_bundle(bundle_dir / "mint.tar.gz", data)

            await register_bundle(conn, instance_id="test", bundle=good)
            await activate_bundle(
                conn, instance_id="test", actor="operator", bundle=good,
                publish=publish, opa_url=opa_url, timeout_seconds=30,
            )
            assert await loaded_bundle_sha256(opa_url) == good.bundle_sha256

            await register_bundle(conn, instance_id="test", bundle=forged)
            await conn.commit()
            with pytest.raises(BundleContentMismatch):
                await activate_bundle(
                    conn, instance_id="test", actor="operator", bundle=forged,
                    publish=publish, opa_url=opa_url, timeout_seconds=8,
                )
            await conn.rollback()
            rows = await (await conn.execute(
                "SELECT id, status FROM mint.policy_versions ORDER BY id"
            )).fetchall()
            assert rows == [("good", "active"), ("tampered", "validated")]
            events = await (await conn.execute(
                "SELECT count(*) FROM mint.events WHERE kind = %s", ("policy_activated",)
            )).fetchone()
            assert events[0] == 1
            await publish(good.archive)
            for _ in range(100):
                try:
                    if await loaded_bundle_sha256(opa_url) == good.bundle_sha256:
                        break
                except BundleContentMismatch:
                    pass
                await asyncio.sleep(0.1)
            assert await loaded_bundle_sha256(opa_url) == good.bundle_sha256

    _run(run)


def test_watch_path_overwrites_injected_version_information(live_database):
    import psycopg

    bundle_dir = Path(os.environ["MINT_TEST_OPA_BUNDLE_DIR"])
    opa_url = os.environ["MINT_TEST_OPA_URL"].rstrip("/")
    prepared = prepare_bundle(ROOT, "version", opa=opa_command(), opa_policy_path="/work/policy")
    forged = dataclasses.replace(prepared, archive=_tampered(
        prepared.archive, "system/version/data.json", lambda _: b'{"version": {"injected": 1}}'
    ))

    async def run():
        async with await psycopg.AsyncConnection.connect(live_database) as conn:
            async def publish(data):
                publish_bundle(bundle_dir / "mint.tar.gz", data)

            await register_bundle(conn, instance_id="test", bundle=forged)
            await activate_bundle(
                conn, instance_id="test", actor="operator", bundle=forged,
                publish=publish, opa_url=opa_url, timeout_seconds=30,
            )
            async with httpx.AsyncClient(trust_env=False) as client:
                reply = await client.get(f"{opa_url}/v1/data/system/version")
            assert set(reply.json()["result"]) == {
                "build_commit", "build_hostname", "build_timestamp", "version"
            }
            assert all(type(v) is str for v in reply.json()["result"].values())

    _run(run)
