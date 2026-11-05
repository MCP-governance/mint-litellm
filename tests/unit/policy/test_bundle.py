import decimal
import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from mint_litellm.plugin.policy.bundle import (
    _validate_hours,
    bundle_content_sha256,
    canonical_json,
    parse_data_json,
    prepare_bundle,
    publish_bundle,
    restore_bundle,
)

ROOT = Path(__file__).resolve().parents[3]


def _independent_hash(bundle_root: Path) -> str:
    data_path = bundle_root / "mint/data/data.json"
    content = b""
    for path in sorted(
        p for p in bundle_root.rglob("*")
        if p.is_file() and p.relative_to(bundle_root).as_posix() != "mint/meta/data.json"
    ):
        body = path.read_bytes()
        if path == data_path:
            body = json.dumps(
                json.loads(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        content += path.relative_to(bundle_root).as_posix().encode() + b"\0" + body + b"\0"
    return hashlib.sha256(content).hexdigest()


def test_bundle_hash_excludes_metadata_and_covers_paths_and_contents(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "policy", tmp_path / "policy")
    monkeypatch.setattr("mint_litellm.plugin.policy.bundle.subprocess.run", lambda *a, **kw: None)
    first = prepare_bundle(tmp_path, "first")
    second = prepare_bundle(tmp_path, "second")
    assert first.bundle_sha256 == second.bundle_sha256
    bundle_root = tmp_path / "policy/bundle"
    assert first.bundle_sha256 == _independent_hash(bundle_root)
    catalog_hash = hashlib.sha256((tmp_path / "policy/catalog.json").read_bytes()).hexdigest()
    assert first.catalog_sha256 == catalog_hash
    with tarfile.open(fileobj=io.BytesIO(first.archive), mode="r:gz") as archive:
        meta = json.load(archive.extractfile("mint/meta/data.json"))
    assert meta == {"policy_version_id": "first", "bundle_sha256": first.bundle_sha256}

    path = bundle_root / "mint/data/data.json"
    path.write_bytes(b"\n " + path.read_bytes().replace(b",", b" , ") + b"\n")
    assert prepare_bundle(tmp_path, "first").bundle_sha256 == first.bundle_sha256
    path.write_bytes(path.read_bytes().replace(b"09:00", b"10:00"))
    assert prepare_bundle(tmp_path, "first").bundle_sha256 != first.bundle_sha256

    rego = bundle_root / "mint/decision.rego"
    before_rego = prepare_bundle(tmp_path, "first").bundle_sha256
    rego.write_bytes(rego.read_bytes() + b"\n")
    assert prepare_bundle(tmp_path, "first").bundle_sha256 != before_rego

    meta_file = bundle_root / "mint/meta/data.json"
    meta_file.parent.mkdir(parents=True)
    meta_file.write_text('{"example": "first"}', encoding="utf-8")
    before_meta = prepare_bundle(tmp_path, "first").bundle_sha256
    meta_file.write_text('{"example": "second"}', encoding="utf-8")
    assert prepare_bundle(tmp_path, "first").bundle_sha256 == before_meta


def test_canonical_json_ignores_layout_and_key_order_but_keeps_number_text():
    left = parse_data_json('{"b": [1, 2.50, "\u00fc"], "a": {"y": null, "x": true}}')
    right = parse_data_json('{ "a":{"x":true,"y":null},\n"b":[1,2.50,"ü"] }')
    assert canonical_json(left) == canonical_json(right)
    assert canonical_json(left) == '{"a":{"x":true,"y":null},"b":[1,2.50,"ü"]}'.encode()
    assert canonical_json(parse_data_json('{"n": 12345678901234567890}')) == (
        b'{"n":12345678901234567890}'
    )
    assert canonical_json(parse_data_json('{"n": 2.5}')) != canonical_json(left)
    with pytest.raises(ValueError):
        parse_data_json('{"n": NaN}')


def test_canonical_numbers_keep_every_digit_and_ignore_the_decimal_context():
    first = parse_data_json('{"n": 0.12345678901234567890123456781}')
    second = parse_data_json('{"n": 0.12345678901234567890123456782}')
    assert canonical_json(first) != canonical_json(second)
    with decimal.localcontext() as context:
        context.prec = 5
        assert canonical_json(first) == b'{"n":0.12345678901234567890123456781}'
        assert canonical_json(first) != canonical_json(second)
    assert canonical_json(parse_data_json('{"n": [1.0, 1e2, -0.0, 1E+30]}')) == (
        b'{"n":[1.0,1e2,-0.0,1E+30]}'
    )


def test_content_hash_separates_paths_and_line_endings():
    data = {"mint/data/data.json": {}}
    base = bundle_content_sha256({"mint/a.rego": "package mint\n"}, data)
    assert base != bundle_content_sha256({"mint/b.rego": "package mint\n"}, data)
    assert base != bundle_content_sha256({"mint/a.rego": "package mint\r\n"}, data)
    with pytest.raises(ValueError):
        bundle_content_sha256({"mint/data/data.json": ""}, data)


def test_extra_data_files_are_rejected(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "policy", tmp_path / "policy")
    monkeypatch.setattr("mint_litellm.plugin.policy.bundle.subprocess.run", lambda *a, **kw: None)
    extra = tmp_path / "policy/bundle/mint/extra/data.json"
    extra.parent.mkdir()
    extra.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        prepare_bundle(tmp_path, "first")


def test_invalid_business_hours_rejected():
    with pytest.raises(ValueError):
        _validate_hours({"tz": "invalid", "days": [1], "start": "09:00", "end": "18:00"})


def test_publish_replaces_complete_archive(tmp_path):
    path = tmp_path / "bundle.tar.gz"
    assert not restore_bundle(path)
    publish_bundle(path, b"first")
    assert not restore_bundle(path)
    publish_bundle(path, b"second")
    assert path.read_bytes() == b"second"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bundle.tar.gz", "bundle.tar.gz.previous"]


def test_restore_returns_the_previously_published_archive(tmp_path):
    path = tmp_path / "bundle.tar.gz"
    publish_bundle(path, b"first")
    publish_bundle(path, b"second")
    assert restore_bundle(path)
    assert path.read_bytes() == b"first"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bundle.tar.gz", "bundle.tar.gz.previous"]


def test_rules_reference_only_policy_data_under_mint():
    """판정 규칙은 ``data.mint`` 밖(특히 ``data.system``)과 ``data`` 전체 순회를 쓰지 않는다."""
    import re

    sources = sorted((ROOT / "policy" / "bundle").rglob("*.rego"))
    assert sources
    for source in sources:
        text = re.sub(r"#[^\n]*", "", source.read_text(encoding="utf-8"))
        for match in re.finditer(r"(?<![\w.])data\b", text):
            rest = text[match.end():]
            assert re.match(r"\s*(?:\.\s*mint\b|\[\s*\"mint\"\s*\])", rest), (
                f"{source.name}: data reference outside data.mint near {rest[:30]!r}"
            )
