"""Protocol tests: drive `python -m harness mcp` over stdio with scripted JSON-RPC, the way Claude Code does."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness import mcp, schema

from .test_core import request

REPO = Path(__file__).resolve().parent.parent


class Session:
    def __init__(self, home):
        env = dict(os.environ, AGENT_HARNESS_HOME=home, AGENT_HARNESS_TEST_PROVIDER="fake",
                   AGENT_HARNESS_INLINE_WORKER="1")
        env.pop("AGENT_HARNESS_DEPTH", None)
        self.proc = subprocess.Popen([sys.executable, "-m", "harness", "mcp"], cwd=REPO, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8")
        self.next_id = 0

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None, notes=None):
        """Send a request; return its response, collecting notifications that arrive before it into notes."""
        self.next_id += 1
        rid = self.next_id
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError("server closed stdout: " + self.proc.stderr.read())
            msg = json.loads(line)
            if msg.get("id") == rid:
                return msg
            if notes is not None:
                notes.append(msg)

    def call(self, name, arguments, token=None, notes=None):
        params = {"name": name, "arguments": arguments}
        if token is not None:
            params["_meta"] = {"progressToken": token}
        resp = self.request("tools/call", params, notes)
        result = resp["result"]
        return json.loads(result["content"][0]["text"]), result.get("isError", False)

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=30)
        self.proc.stdout.close()
        self.proc.stderr.close()


class Protocol(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Session(self.tmp.name)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def handshake(self):
        return self.s.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                             "clientInfo": {"name": "test", "version": "0"}})

    def test_discover_then_legacy_handshake(self):
        resp = self.s.request("server/discover", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}})
        self.assertEqual(resp["error"]["code"], -32601)
        # Claude Code's initialize arrived without a jsonrpc field (docs/spikes.md); accept it.
        self.s.send({"id": "init", "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        resp = json.loads(self.s.proc.stdout.readline())
        self.assertEqual(resp["id"], "init")
        info = resp["result"]
        self.assertEqual(info["protocolVersion"], "2025-11-25")
        self.assertEqual(info["capabilities"], {"tools": {}})
        self.assertLessEqual(len(info["instructions"]), 2048)
        self.assertIn("untrusted", info["instructions"])
        self.s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(self.s.request("ping")["result"], {})

    def test_unknown_version_gets_latest_legacy(self):
        self.s.next_id = 0
        resp = self.s.request("initialize", {"protocolVersion": "2099-01-01"})
        self.assertEqual(resp["result"]["protocolVersion"], mcp.LEGACY_VERSIONS[0])

    def test_tools_list_schemas(self):
        self.handshake()
        tools = {t["name"]: t for t in self.s.request("tools/list")["result"]["tools"]}
        self.assertEqual(set(tools), {"delegate", "run_result", "run_reply", "run_label", "runs_report", "doctor"})
        for t in tools.values():
            self.assertLessEqual(len(t["description"]), 2048)
            self.assertEqual(schema.check_schema(t["inputSchema"], mcp_names=True), [], t["name"])
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertNotIn("anyOf", json.dumps(t["inputSchema"]))
        self.assertEqual(tools["delegate"]["inputSchema"]["required"],
                         schema.load("request.schema.json")["required"])

    def test_delegate_with_progress_then_result_label_report(self):
        self.handshake()
        notes = []
        res, is_error = self.s.call("delegate", request(), token=7, notes=notes)
        self.assertFalse(is_error)
        self.assertEqual(res["state"], "done")
        self.assertEqual(schema.validate_result(res, "review"), [])
        progress = [n for n in notes if n.get("method") == "notifications/progress"]
        self.assertTrue(progress)
        self.assertEqual(progress[0]["params"]["progressToken"], 7)
        self.assertIn(res["run_id"], progress[0]["params"]["message"])

        again, _ = self.s.call("run_result", {"run_id": res["run_id"]})
        self.assertEqual(again["run_id"], res["run_id"])
        labelled, _ = self.s.call("run_label", {"run_id": res["run_id"], "outcome": "useful"})
        self.assertEqual(labelled["label"]["outcome"], "useful")
        rep, _ = self.s.call("runs_report", {})
        self.assertEqual(rep["totals"]["runs"], 1)

    def test_blocked_then_reply(self):
        self.handshake()
        res, _ = self.s.call("delegate", request(model="fake:blocked_then_done"))
        self.assertEqual(res["state"], "blocked")
        done, is_error = self.s.call("run_reply", {"run_id": res["run_id"], "text": "Go ahead."})
        self.assertFalse(is_error)
        self.assertEqual(done["state"], "done")

    def test_errors_are_tool_errors(self):
        self.handshake()
        out, is_error = self.s.call("delegate", request(type="execute"))
        self.assertTrue(is_error)
        self.assertEqual(out["error"]["kind"], "invalid_request")
        out, is_error = self.s.call("run_result", {"run_id": "no-such-run"})
        self.assertTrue(is_error)
        resp = self.s.request("tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(resp["error"]["code"], -32602)
        resp = self.s.request("resources/list")
        self.assertEqual(resp["error"]["code"], -32601)

    def test_parse_error_and_server_survives(self):
        self.handshake()
        self.s.proc.stdin.write("{not json\n")
        self.s.proc.stdin.flush()
        resp = json.loads(self.s.proc.stdout.readline())
        self.assertEqual(resp["error"]["code"], -32700)
        self.assertEqual(self.s.request("ping")["result"], {})

    def test_concurrent_calls(self):
        self.handshake()
        # Two calls in flight: the slow one must not block the quick one.
        self.s.send({"jsonrpc": "2.0", "id": 100, "method": "tools/call",
                     "params": {"name": "delegate", "arguments": request(model="fake:slow", task="slow")}})
        self.s.send({"jsonrpc": "2.0", "id": 101, "method": "ping"})
        first = json.loads(self.s.proc.stdout.readline())
        self.assertEqual(first["id"], 101)
        while True:
            msg = json.loads(self.s.proc.stdout.readline())
            if msg.get("id") == 100:
                break
        self.assertEqual(json.loads(msg["result"]["content"][0]["text"])["state"], "done")


if __name__ == "__main__":
    unittest.main()
