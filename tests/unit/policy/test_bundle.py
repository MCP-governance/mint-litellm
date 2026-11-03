import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from mint_litellm.plugin.policy.bundle import _validate_hours, prepare_bundle, publish_bundle

ROOT = Path(__file__).resolve().parents[3]


def test_bundle_hash_excludes_metadata_and_covers_paths_and_contents(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "policy", tmp_path / "policy")
    monkeypatch.setattr("mint_litellm.plugin.policy.bundle.subprocess.run", lambda *a, **kw: None)
    first = prepare_bundle(tmp_path, "first")
    second = prepare_bundle(tmp_path, "second")
    assert first.bundle_sha256 == second.bundle_sha256
    catalog_hash = hashlib.sha256((tmp_path / "policy/catalog.json").read_bytes()).hexdigest()
    assert first.catalog_sha256 == catalog_hash
    with tarfile.open(fileobj=io.BytesIO(first.archive), mode="r:gz") as archive:
        meta = json.load(archive.extractfile("mint/meta/data.json"))
    assert meta == {"policy_version_id": "first", "bundle_sha256": first.bundle_sha256}
    path = tmp_path / "policy/bundle/mint/data/data.json"
    path.write_bytes(path.read_bytes() + b" ")
    assert prepare_bundle(tmp_path, "first").bundle_sha256 != first.bundle_sha256


def test_invalid_business_hours_rejected():
    with pytest.raises(ValueError):
        _validate_hours({"tz": "invalid", "days": [1], "start": "09:00", "end": "18:00"})


def test_publish_replaces_complete_archive(tmp_path):
    path = tmp_path / "bundle.tar.gz"
    publish_bundle(path, b"first")
    publish_bundle(path, b"second")
    assert path.read_bytes() == b"second"
    assert len(list(tmp_path.iterdir())) == 1
