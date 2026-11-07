import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_catalog_lists_every_rule_the_bundle_can_apply():
    rego = (ROOT / "policy/bundle/mint/policy.rego").read_text(encoding="utf-8")
    ids = set(re.findall(r'"((?:POL-[A-Z]-\d\d)|R\d)"', rego))
    catalog = json.loads((ROOT / "policy/catalog.json").read_text(encoding="utf-8"))
    assert {rule["id"] for rule in catalog["rules"]} == ids
    for rule in catalog["rules"]:
        if rule["id"].startswith("POL-"):
            assert rule["observable"] is True and rule["output"] == "Block"
