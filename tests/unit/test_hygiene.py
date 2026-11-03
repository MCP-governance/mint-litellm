"""저장소에 내부 작업 흔적이 들어가지 않도록 텍스트 파일을 검사한다."""

import re
from pathlib import Path

import pytest

import mint_litellm

ROOT = Path(mint_litellm.__file__).resolve().parents[2]
SUFFIXES = {".py", ".sql", ".md", ".toml", ".yml", ".yaml", ".txt", ".cfg", ".ini"}
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
}

FORBIDDEN = {
    "section sign": re.compile("§"),
    "decision or task id": re.compile(r"\b(?:DS|DL|V|P|N)-\d+[a-z]?\b"),
    "work memo words": re.compile("일단|나중에|TODO|FIXME|XXX"),
    "internal documents": re.compile("위키|wiki|지시서|설계서", re.IGNORECASE),
    "dated trace": re.compile(r"\b20\d\d-\d\d-\d\d\b|\b1[01]/\d\d\b"),
    "ip address": re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
}


def repo_files():
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.is_file() and path.suffix in SUFFIXES and path.name != Path(__file__).name:
            yield path


@pytest.mark.parametrize("label", sorted(FORBIDDEN))
def test_no_internal_traces(label):
    pattern = FORBIDDEN[label]
    hits = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in repo_files()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert not hits, hits
