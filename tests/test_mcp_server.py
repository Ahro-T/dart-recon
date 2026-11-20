import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"  # 켐트로닉스(00371485) 2024·2025 CFS, 공개 DART 응답


def rpc(*messages: dict) -> list[dict]:
    env = {**os.environ, "DART_RECON_DATA": str(FIXTURES)}
    env.pop("DART_OFFLINE", None)  # 서버가 스스로 오프라인으로 강제해야 한다
    stdin = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
    out = subprocess.run([sys.executable, "-m", "dart_recon.mcp_server"], input=stdin, capture_output=True,
                         text=True, encoding="utf-8", cwd=ROOT, env=env, timeout=60, check=True).stdout
    return [json.loads(line) for line in out.splitlines()]


def call(name: str, arguments: dict) -> dict:
    return rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})[0]


def payload(resp: dict) -> dict:
    return json.loads(resp["result"]["content"][0]["text"])


class Protocol(unittest.TestCase):
    def test_initialize_and_notification(self):
        out = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                  {"jsonrpc": "2.0", "method": "notifications/initialized"},
                  {"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual([r["id"] for r in out], [1, 2])  # 알림에는 응답이 없다
        self.assertEqual(out[0]["result"]["capabilities"], {"tools": {}})
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(out[1]["result"], {})

    def test_tools_list_is_exactly_five(self):
        tools = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0]["result"]["tools"]
        self.assertEqual({t["name"] for t in tools},
                         {"dart_company", "dart_check", "dart_explain", "dart_history", "dart_rules"})
        self.assertEqual(len(tools), 5)

    def test_unknown_method(self):
        self.assertEqual(rpc({"jsonrpc": "2.0", "id": 1, "method": "nope"})[0]["error"]["code"], -32601)

    def test_bad_args(self):
        self.assertEqual(call("dart_check", {"corp": "켐트로닉스"})["error"]["code"], -32602)  # year 누락
        self.assertEqual(call("dart_check", {"corp": "켐트로닉스", "year": "2025"})["error"]["code"], -32602)
        self.assertEqual(call("dart_check", {"corp": "켐트로닉스", "year": 2025, "fs_div": "XX"})["error"]["code"], -32602)
        self.assertEqual(call("no_such_tool", {})["error"]["code"], -32602)


class Tools(unittest.TestCase):
    def test_company(self):
        r = payload(call("dart_company", {"query": "켐트로닉스"}))
        self.assertEqual(r["matches"][0]["cached_filings"], {"2024": ["CFS"], "2025": ["CFS"]})
        self.assertEqual(payload(call("dart_company", {"query": "089010"}))["total_matches"], 1)

    def test_check_findings(self):
        r = payload(call("dart_check", {"corp": "켐트로닉스", "year": 2025}))
        self.assertTrue(r["restatement_checked"])
        self.assertEqual(r["fs_div"], "CFS")
        self.assertEqual(sum(r["summary"].values()), len(r["findings"]))
        self.assertTrue(all(f["rule"].startswith("KDQ-") for f in r["findings"]))
        self.assertEqual({f["rule"] for f in r["findings"]},
                         {"KDQ-0001", "KDQ-0002", "KDQ-0003", "KDQ-0004", "KDQ-0005", "KDQ-0006"})
        statuses = [f["status"] for f in r["findings"]]
        self.assertEqual(statuses[0], "mismatch")  # 불일치가 앞
        self.assertEqual(statuses, sorted(statuses, key=["mismatch", "reclassified", "rounding", "missing", "ok"].index))
        bad = [f for f in r["findings"] if f["rule"] == "KDQ-0004" and f["status"] == "mismatch"]
        self.assertTrue(bad)
        self.assertEqual(bad[0]["diff"], bad[0]["lhs"] - bad[0]["rhs"])

    def test_explain(self):
        r = payload(call("dart_explain", {"corp": "켐트로닉스", "year": 2025}))
        self.assertGreater(r["non_ok_count"], 0)
        for item in r["items"]:
            self.assertTrue(item["evidence"])
            self.assertEqual({"account_nm", "account_id", "sj_div", "thstrm_amount", "frmtrm_amount"} - set(item["evidence"][0]), set())
        restated = [i for i in r["items"] if i["finding"]["rule"] == "KDQ-0006"]
        self.assertEqual({e["filing"] for e in restated[0]["evidence"]}, {"2025년 보고서", "2024년 보고서"})
        self.assertEqual(restated[0]["cause_candidate"]["category"], "restatement")

    def test_history(self):
        r = payload(call("dart_history", {"corp": "00371485", "account": "assets"}))
        years = {y["fiscal_year"]: y for y in r["series"][0]["years"]}
        self.assertEqual(set(years), {2023, 2024, 2025})
        self.assertEqual(years[2024]["first_reported_in"], 2024)
        self.assertEqual(years[2024]["latest_reported_in"], 2025)
        by_id = payload(call("dart_history", {"corp": "켐트로닉스", "account": "ifrs-full_Assets"}))
        self.assertEqual(by_id["series"][0]["years"], r["series"][0]["years"])

    def test_rules(self):
        listed = payload(call("dart_rules", {}))["rules"]
        self.assertEqual([x["rule"] for x in listed], [f"KDQ-000{i}" for i in range(1, 7)])
        full = payload(call("dart_rules", {"rule_id": "KDQ-0006"}))
        self.assertIn("KDQ-0006", full["markdown"])

    def test_tool_errors_are_is_error(self):
        for name, args in (("dart_check", {"corp": "없는회사", "year": 2025}),
                           ("dart_check", {"corp": "켐트로닉스", "year": 2019}),
                           ("dart_history", {"corp": "켐트로닉스", "account": "nope"}),
                           ("dart_rules", {"rule_id": "KDQ-9999"})):
            resp = call(name, args)
            self.assertTrue(resp["result"]["isError"], name)
            self.assertEqual(resp["result"]["content"][0]["type"], "text")


if __name__ == "__main__":
    unittest.main()
