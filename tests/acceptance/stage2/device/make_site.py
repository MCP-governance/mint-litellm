# ruff: noqa: E501
"""장비에서 실행: site.example.json을 바탕으로 이 장비의 site.json을 만든다(.runtime, 커밋하지 않음).

환경변수는 acc.env를 읽은 상태에서 실행한다. 사용: python make_site.py > tests/acceptance/stage2/.runtime/site.json
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
site = json.loads((HERE.parent / "site.example.json").read_text(encoding="utf-8"))
frag = json.loads(subprocess.run(
    [sys.executable, "-m", "tests.acceptance.stage2.adapters", "emit-site", "--repo", str(REPO)],
    cwd=REPO, capture_output=True, text=True, check=True).stdout)
site.update(frag)
site["instance_id"] = "acceptance"
site["api"] = "http://127.0.0.1:4330"
site["litellm_admin"] = "http://127.0.0.1:4300"
site["litellm"] = ["http://127.0.0.1:4300", "http://127.0.0.1:4301"]
site["plain"] = "http://127.0.0.1:4310"
site["cli"] = [sys.executable, "-m", "mint_litellm.cli"]
site["secret_env"] = {
    "admin": "ACC_ADMIN_TOKEN", "approver": "ACC_APPROVER_TOKEN", "caller": "ACC_CALLER_KEY",
    "control": "ACC_CONTROL_KEY", "db": "ACC_DATABASE_URL", "export_hmac": "MINT_EXPORT_HMAC_KEY",
    "admin2": "ACC_ADMIN2_TOKEN", "submit": "ACC_SUBMIT_KEY", "litellm_admin": "ACC_LITELLM_ADMIN_KEY",
    "plugin_db": "ACC_PLUGIN_DATABASE_URL", "staff_db": "ACC_STAFF_DATABASE_URL",
}
site["intake"].update(org_id="default", owner_user_id="acc-owner")
for name, entry in site["servers"].items():
    entry["server_id"] = name
    site["counters"][name] = "http://127.0.0.1:4350/" + name
site["decision_logs"] = [["docker", "logs", "mints2-litellm-1"], ["docker", "logs", "mints2-litellm2-1"]]
site["artifacts"]["export_dir"] = str(Path.home() / "mint-s2" / "export")
site["artifacts"]["client_pj1"] = str(HERE.parent / ".runtime" / "client-pj1.json")
site["artifacts"]["client_pj3"] = str(HERE.parent / ".runtime" / "client-pj3.json")
print(json.dumps(site, ensure_ascii=False, indent=2))
