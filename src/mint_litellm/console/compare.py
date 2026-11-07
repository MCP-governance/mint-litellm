"""계약 드리프트 비교: 승인본과 관측본을 항목별로 맞추고, 선택한 항목을 줄 단위로 대조한다."""

import difflib
import json
from typing import Any

DEPLOYMENT = "(배포)"


def _pretty(text: str | None) -> list[str]:
    if text is None:
        return []
    try:
        value = json.loads(text)
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    except ValueError:
        return text.split("\n")


def changes(info: dict[str, Any]) -> list[dict[str, Any]]:
    """``contract diff`` 결과에서 추가·삭제·변경된 항목(도구와 배포)을 낸다."""
    approved = {
        (c["credential_scope"], c["tool"]): c for c in info["tools"] if c["status"] == "approved"
    }
    observed = {
        (o["scope"], o["name"]): o for o in info["observations"] if o["kind"] == "tool"
    }
    rows = []
    for key in sorted(set(approved) | set(observed)):
        a, o = approved.get(key), observed.get(key)
        if a is not None and (o is None or a["drift"] == "removed"):
            kind = "removed"
        elif a is None:
            kind = "added"
        elif a["canonical_text"] != o["canonical_text"]:
            kind = "changed"
        else:
            continue
        rows.append({
            "scope": key[0], "name": key[1], "change": kind,
            "approved": a["canonical_text"] if a else None,
            "observed": o["canonical_text"] if o else None,
        })
    deployment = next((d for d in info["deployments"] if d["status"] == "approved"), None)
    seen = next((o for o in info["observations"] if o["kind"] == "deployment"), None)
    if deployment is not None and seen is not None and (
        deployment["canonical_text"] != seen["canonical_text"]
    ):
        rows.append({
            "scope": "", "name": DEPLOYMENT, "change": "changed",
            "approved": deployment["canonical_text"], "observed": seen["canonical_text"],
        })
    return rows


def side_by_side(approved: str | None, observed: str | None) -> dict[str, list[tuple[str, bool]]]:
    """줄마다 (내용, 바뀐 줄 여부). 바뀐 줄만 강조한다."""
    left, right = _pretty(approved), _pretty(observed)
    match = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    out_left: list[tuple[str, bool]] = []
    out_right: list[tuple[str, bool]] = []
    for tag, i1, i2, j1, j2 in match.get_opcodes():
        same = tag == "equal"
        out_left += [(line, not same) for line in left[i1:i2]]
        out_right += [(line, not same) for line in right[j1:j2]]
    return {"approved": out_left, "observed": out_right}
