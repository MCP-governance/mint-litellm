"""정책 활성화 호출의 연결: 직전 번들 보관·복원 콜백과 복원 결과 전달."""

import asyncio

import pytest

pytest.importorskip("psycopg")

from mint_litellm.control import policy_versions  # noqa: E402
from mint_litellm.control.errors import ControlError  # noqa: E402
from mint_litellm.plugin.policy import bundle as policy_bundle  # noqa: E402


class Prepared:
    policy_version_id = "v2"
    bundle_sha256 = "b" * 64
    catalog_sha256 = "c" * 64
    archive = b"archive"


@pytest.fixture
def wired(monkeypatch, fresh_db, tmp_path):
    """번들 준비·게시·활성화 함수를 기록하는 자리로 바꾸고, 보관·복원 호출 인자를 모은다."""
    calls: dict[str, list] = {"keep": [], "restore": [], "activate": [], "register": []}
    monkeypatch.setattr(policy_bundle, "prepare_bundle", lambda *a, **k: Prepared())
    monkeypatch.setattr(policy_bundle, "publish_bundle", lambda path, data: None)
    monkeypatch.setattr(
        policy_bundle, "keep_active_bundle", lambda path, v, s: calls["keep"].append((path, v, s))
    )

    def restore(path, version, sha):
        calls["restore"].append((path, version, sha))
        return True

    monkeypatch.setattr(policy_bundle, "restore_bundle", restore)

    async def register(conn, *, instance_id, bundle):
        calls["register"].append(bundle.policy_version_id)

    monkeypatch.setattr(policy_bundle, "register_bundle", register)
    source = policy_versions.PolicySource(
        root=tmp_path, opa="opa", bundle_path=tmp_path / "mint.tar.gz",
        opa_url="http://opa.test:8181", timeout_seconds=5,
    )
    return calls, source, fresh_db


def install(monkeypatch, name, behavior):
    async def fake(conn, **kwargs):
        await behavior(kwargs)

    monkeypatch.setattr(policy_bundle, name, fake)


def run(conn, url, source):
    return policy_versions.activate(conn, url, "inst1", "boss", "v2", source)


@pytest.fixture
def sync_conn(fresh_db):
    import psycopg

    with psycopg.connect(fresh_db) as conn:
        yield conn


def test_keep_and_restore_are_passed_through_with_the_bundle_path(wired, sync_conn, monkeypatch):
    calls, source, url = wired

    async def behavior(kwargs):
        calls["activate"].append(sorted(kwargs))
        await kwargs["keep"]("v1", "a" * 64)
        assert await kwargs["restore"]("v1", "a" * 64) is True

    install(monkeypatch, "activate_bundle", behavior)
    run(sync_conn, url, source)
    assert calls["register"] == ["v2"]
    assert "keep" in calls["activate"][0] and "restore" in calls["activate"][0]
    assert calls["keep"] == [(source.bundle_path, "v1", "a" * 64)]
    assert calls["restore"] == [(source.bundle_path, "v1", "a" * 64)]


@pytest.mark.parametrize("failure", ["timeout", "cancelled", "mismatch", "oserror"])
def test_the_restore_outcome_of_a_failed_activation_reaches_the_error(
    wired, sync_conn, monkeypatch, failure
):
    calls, source, url = wired
    outcome = {"timeout": policy_bundle.RESTORED, "cancelled": policy_bundle.RESTORE_INTERRUPTED,
               "mismatch": policy_bundle.NOT_RESTORED_NO_COPY,
               "oserror": policy_bundle.NOT_RESTORED_UNKNOWN}[failure]

    async def behavior(kwargs):
        await kwargs["restore"]("v1", "a" * 64)
        if failure == "timeout":
            cancelled = asyncio.CancelledError()
            cancelled.restore_outcome = outcome  # 바깥 제한은 취소를 TimeoutError로 바꾼다
            raise TimeoutError("outer limit") from cancelled
        if failure == "cancelled":
            cancelled = asyncio.CancelledError()
            cancelled.restore_outcome = outcome
            raise cancelled
        error = (policy_bundle.BundleContentMismatch("loaded content differs")
                 if failure == "mismatch" else OSError("connection lost"))
        error.restore_outcome = outcome
        raise error

    install(monkeypatch, "activate_bundle", behavior)
    with pytest.raises(policy_versions.ActivationError) as raised:
        run(sync_conn, url, source)
    assert raised.value.restore_outcome == outcome
    assert f"직전 번들: {outcome}" in str(raised.value)
    assert calls["restore"]


def test_a_failure_without_an_outcome_is_reported_without_one(wired, sync_conn, monkeypatch):
    _, source, url = wired

    async def behavior(kwargs):
        raise ValueError("policy version does not match validated bundle")

    install(monkeypatch, "activate_bundle", behavior)
    with pytest.raises(policy_versions.ActivationError) as raised:
        run(sync_conn, url, source)
    assert raised.value.restore_outcome is None and "직전 번들" not in str(raised.value)
    assert isinstance(raised.value, ControlError)


def test_a_registered_version_is_retried_with_the_same_callbacks(wired, sync_conn, monkeypatch):
    calls, source, url = wired
    sync_conn.execute(
        "INSERT INTO mint.policy_versions (instance_id, id, catalog_sha256, bundle_sha256)"
        " VALUES ('inst1', 'v2', %s, %s)", ("c" * 64, "b" * 64))

    async def behavior(kwargs):
        await kwargs["keep"]("v1", "a" * 64)
        await kwargs["restore"]("v1", "a" * 64)
        calls["activate"].append("retry")

    install(monkeypatch, "retry_activation", behavior)
    run(sync_conn, url, source)
    assert calls["activate"] == ["retry"] and calls["register"] == []
    assert calls["keep"] and calls["restore"]


def test_restore_outcome_is_read_through_the_cause_and_context_chain():
    inner = asyncio.CancelledError()
    inner.restore_outcome = "restored"
    outer = TimeoutError("x")
    outer.__cause__ = inner
    assert policy_versions.restore_outcome(outer) == "restored"
    context = ValueError("y")
    context.__context__ = outer
    assert policy_versions.restore_outcome(context) == "restored"
    assert policy_versions.restore_outcome(ValueError("z")) is None
    assert policy_versions.restore_outcome(None) is None
    loop = ValueError("loop")
    loop.__cause__ = loop
    assert policy_versions.restore_outcome(loop) is None


