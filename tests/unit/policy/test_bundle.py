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
    keep_active_bundle,
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


def _archive(name: str, *, content: str | None = None) -> tuple[bytes, str]:
    """내용이 ``content``(기본은 ``name``)인 작은 번들 아카이브와 그 내용 해시."""
    body = ("package p" + chr(10) + "# " + (content or name) + chr(10)).encode()
    sha = bundle_content_sha256({"p.rego": body}, {})
    meta = json.dumps({"policy_version_id": name, "bundle_sha256": sha}).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for member, data in (("p.rego", body), ("mint/meta/data.json", meta)):
            info = tarfile.TarInfo(member)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return stream.getvalue(), sha


def test_publish_replaces_complete_archive(tmp_path):
    path = tmp_path / "bundle.tar.gz"
    publish_bundle(path, b"first")
    publish_bundle(path, b"second")
    assert path.read_bytes() == b"second"
    assert [p.name for p in tmp_path.iterdir()] == ["bundle.tar.gz"]


def test_keep_and_restore_are_bound_to_the_active_bundle(tmp_path):
    path = tmp_path / "bundle.tar.gz"
    (a, sha_a), (b, _), (c, _) = _archive("a"), _archive("b"), _archive("c")
    assert not restore_bundle(path, "a", sha_a)
    publish_bundle(path, a)
    keep_active_bundle(path, "a", sha_a)
    publish_bundle(path, b)
    assert restore_bundle(path, "a", sha_a)
    assert path.read_bytes() == a
    # 현재 파일이 활성 판본이 아니면 보관본을 덮지 않는다(실패한 b가 남은 상태에서 재시도)
    publish_bundle(path, b)
    keep_active_bundle(path, "a", sha_a)
    publish_bundle(path, c)
    keep_active_bundle(path, "a", sha_a)
    assert restore_bundle(path, "a", sha_a) and path.read_bytes() == a


def test_restore_rejects_stale_or_altered_copies(tmp_path):
    path = tmp_path / "bundle.tar.gz"
    previous = tmp_path / "bundle.tar.gz.previous"
    (a, sha_a), (z, _) = _archive("a"), _archive("z")
    publish_bundle(path, a)
    previous.write_bytes(z)
    assert not restore_bundle(path, "a", sha_a)
    previous.write_bytes(b"not an archive")
    assert not restore_bundle(path, "a", sha_a)
    # 같은 판본 ID·메타지만 내용이 바뀐 아카이브
    altered, _ = _archive("a", content="changed")
    previous.write_bytes(_with_meta(altered, "a", sha_a))
    assert not restore_bundle(path, "a", sha_a)
    assert path.read_bytes() == a
    keep_active_bundle(path, "a", sha_a)
    assert restore_bundle(path, "a", sha_a)


def _with_meta(archive: bytes, name: str, sha: str) -> bytes:
    stream = io.BytesIO()
    meta = json.dumps({"policy_version_id": name, "bundle_sha256": sha}).encode()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source, tarfile.open(
        fileobj=stream, mode="w:gz"
    ) as target:
        for member in source.getmembers():
            data = source.extractfile(member).read()
            if member.name == "mint/meta/data.json":
                data = meta
            info = tarfile.TarInfo(member.name)
            info.size = len(data)
            target.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def _data_violations(text: str) -> list[str]:
    """``data`` 참조 가운데 ``data.mint``나 ``data["mint"]``가 아닌 것. 주석과 문자열을 구분한다."""
    import re

    tokens = [
        t for t in re.findall(r'"(?:\\.|[^"\\])*"|`[^`]*`|#[^\n]*|[A-Za-z_]\w*|\S', text)
        if not t.startswith("#")
    ]
    found = []
    for i, token in enumerate(tokens):
        if token != "data" or (i and tokens[i - 1] == "."):
            continue
        following = tokens[i + 1 : i + 5]
        if following[:2] == [".", "mint"] or following[:3] == ["[", '"mint"', "]"]:
            continue
        found.append(" ".join(tokens[i : i + 5]))
    return found


@pytest.mark.parametrize(
    "text",
    [
        'probe := ["#", data.system.version]',
        'x := data["system"]',
        "x := data . system",
        "y := walk(data, p)",
        "y := data[key]",
        "z := data",
        "import data.system",
        "x := `#` + data.system.bundles",
    ],
)
def test_data_reference_check_catches_bypasses(text):
    assert _data_violations(text)


@pytest.mark.parametrize(
    "text",
    ["x := data.mint.data # data.system", 'x := data["mint"].a', "x := input.data.y",
     'x := "data.system"', "import data.mint.rules"],
)
def test_data_reference_check_allows_mint_data(text):
    assert not _data_violations(text)


def test_rules_reference_only_policy_data_under_mint():
    """판정 규칙은 ``data.mint`` 밖(특히 ``data.system``)과 ``data`` 전체 순회를 쓰지 않는다."""
    sources = sorted((ROOT / "policy" / "bundle").rglob("*.rego"))
    assert sources
    for source in sources:
        assert not _data_violations(source.read_text(encoding="utf-8")), source.name
