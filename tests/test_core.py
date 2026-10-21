import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness import core, quota, schema, store
from harness.adapters import codex as codex_adapter

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "v0-runs"
REPO = Path(__file__).resolve().parent.parent


def request(**overrides):
    req = {"workdir": str(REPO), "type": "review", "task": "Test review", "objective": "Find bugs.",
           "inputs": ["README.md"], "criteria": ["Bugs"], "source_of_truth": ["intended behaviour"],
           "model": "fake:done"}
    req.update(overrides)
    return req


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def snapshot(*percents, resets="2026-11-09T01:53:38-05:00"):
    names = ("primary", "secondary")
    return {"provider": "fake", "plan_type": "free", "taken_at": None,
            "windows": [{"name": names[i], "used_percent": p, "window_minutes": 43200, "resets_at": resets}
                        for i, p in enumerate(percents)]}


class StoreCase(unittest.TestCase):
    """Each test gets its own store; workers run in-process unless a test spawns them."""
    inline = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {"AGENT_HARNESS_HOME": self.tmp.name}
        if self.inline:
            env["AGENT_HARNESS_INLINE_WORKER"] = "1"
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop("AGENT_HARNESS_DEPTH", None)
        os.environ.pop("AGENT_HARNESS_FAKE_QUOTA", None)
        if not self.inline:
            os.environ.pop("AGENT_HARNESS_INLINE_WORKER", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def set_quota(self, snap):
        path = Path(self.tmp.name) / "fake-quota.json"
        path.write_text(json.dumps(snap), encoding="utf-8")
        os.environ["AGENT_HARNESS_FAKE_QUOTA"] = str(path)

    def run_fake(self, scenario, **overrides):
        res = core.delegate(request(model=f"fake:{scenario}", **overrides), provider="fake")
        self.assertEqual(schema.validate_result(res, overrides.get("type", "review")), [], res)
        return res


class Parser(unittest.TestCase):
    def test_replays_real_v0_event_streams(self):
        for rdir in sorted(FIXTURES.iterdir()):
            meta = json.loads((rdir / "meta.json").read_text(encoding="utf-8"))
            parsed = codex_adapter.parse_turn(rdir, 1)
            turn = meta["turns"][0]
            self.assertEqual(parsed["thread_id"], meta["thread_id"], rdir.name)
            self.assertEqual(parsed["usage"]["input_tokens"], turn["usage"]["input_tokens"], rdir.name)
            self.assertEqual(parsed["usage"]["reasoning_tokens"], turn["usage"]["reasoning_output_tokens"])
            self.assertEqual(json.loads(parsed["answer_text"])["status"], turn["status"])

    def test_counts_commands_blocked_by_policy(self):
        parsed = codex_adapter.parse_turn(FIXTURES / "20261009-155826-review-codex-delegate-py-for-correctness", 1)
        self.assertGreater(parsed["commands_blocked"], 0)

    def test_rate_limit_snapshot_both_windows(self):
        rl = {"primary": {"used_percent": 4.0, "window_minutes": 43200, "resets_at": 1794167618},
              "secondary": {"used_percent": 60.0, "window_minutes": 300, "resets_at": 1794167618},
              "plan_type": "free"}
        snap = codex_adapter.snapshot_from_rate_limits(rl)
        self.assertEqual([w["name"] for w in snap["windows"]], ["primary", "secondary"])
        self.assertEqual(quota.worst_window(snap)["name"], "secondary")
        rl["secondary"] = None
        self.assertEqual(len(codex_adapter.snapshot_from_rate_limits(rl)["windows"]), 1)

    def test_latest_snapshot_from_rollout(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rollout-x.jsonl"
            lines = [{"timestamp": f"t{i}", "payload": {"rate_limits": {"primary": {"used_percent": p,
                      "window_minutes": 1, "resets_at": 1794167618}, "secondary": None, "plan_type": "free"}}}
                     for i, p in enumerate((3.0, 4.0))]
            path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")
            snap = codex_adapter.latest_snapshot(path)
            self.assertEqual((snap["windows"][0]["used_percent"], snap["taken_at"]), (4.0, "t1"))

    def test_harness_mcp_detection(self):
        cfg = '[mcp_servers.other]\ncommand = "x"\n[mcp_servers.ah]\ncommand = "python"\nargs = ["-m", "harness", "mcp"]\n'
        self.assertEqual(codex_adapter.harness_mcp_registered(cfg), "ah")
        self.assertIsNone(codex_adapter.harness_mcp_registered('[mcp_servers.other]\ncommand = "x"\n[x]\nharness = 1'))

    def mcp_list(self, stdout, returncode=0):
        done = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")
        return (mock.patch.object(codex_adapter.CodexAdapter, "exe", return_value="codex"),
                mock.patch.object(codex_adapter.subprocess, "run", return_value=done))

    def test_command_turns_off_every_mcp_server(self):
        a = codex_adapter.CodexAdapter()
        with tempfile.TemporaryDirectory() as d:
            run = {"run_dir": d, "workdir": d, "type": "review", "model": "m", "effort": "high", "thread_id": "t"}
            exe, listing = self.mcp_list(json.dumps([{"name": "docs"}, {"name": "my-db"}]))
            with exe, listing:
                for turn in (1, 2):
                    argv, _ = a.command(run, turn)
                    self.assertIn("mcp_servers.docs.enabled=false", argv)
                    self.assertIn("mcp_servers.my-db.enabled=false", argv)

    def test_mcp_listing_fails_closed(self):
        a = codex_adapter.CodexAdapter()
        for stdout, code in (("not json", 0), ("[]", 1), (json.dumps([{"name": "a.b"}]), 0), ("{}", 0)):
            exe, listing = self.mcp_list(stdout, code)
            with exe, listing, self.assertRaises(codex_adapter.ProviderUnavailable):
                a.mcp_servers(".")


class States(StoreCase):
    def test_done(self):
        res = self.run_fake("done")
        self.assertEqual((res["state"], res["error"], res["truncated"]), ("done", None, False))
        self.assertEqual(len(res["answer"]["findings"]), 1)
        self.assertEqual(res["prompt_version"], "fake-1")
        self.assertEqual(res["usage"]["input_tokens"], 89823)
        rdir = Path(res["run_dir"])
        for name in ("request.json", "prompt.md", "events.jsonl", "stderr.log", "result.json", "meta.json"):
            self.assertTrue((rdir / name).exists(), name)
        self.assertFalse((rdir / "lock").exists())

    def test_blocked(self):
        res = self.run_fake("blocked")
        self.assertEqual(res["state"], "blocked")
        self.assertTrue(res["answer"]["questions"])
        self.assertTrue(any("blocked by the provider's sandbox" in w for w in res["warnings"]))

    def test_research(self):
        res = self.run_fake("research", type="research")
        self.assertEqual(res["state"], "done")
        self.assertNotIn("severity", res["answer"]["findings"][0])

    def test_failed(self):
        res = self.run_fake("failed")
        self.assertEqual(res["state"], "failed")
        self.assertEqual(res["error"], {"kind": "provider_error", "message": "model overloaded"})

    def test_schema_invalid(self):
        res = self.run_fake("bad_json")
        self.assertEqual(res["error"]["kind"], "schema_invalid")

    def test_timed_out(self):
        cfg = dict(store.DEFAULT_CONFIG, default_timeout_s=1)
        with mock.patch.object(store, "config", return_value=cfg):
            res = self.run_fake("hang")
        self.assertEqual((res["state"], res["error"]["kind"]), ("timed_out", "timeout"))
        self.assertLess(res["timing"]["seconds"], 30)

    def test_refused_at_98_percent(self):
        self.set_quota(snapshot(10.0, 98.0))
        res = self.run_fake("done")
        self.assertEqual((res["state"], res["error"]["kind"]), ("refused", "quota_refused"))
        self.assertIn("secondary", res["error"]["message"])
        self.assertIsNone(res["answer"])
        self.assertFalse((Path(res["run_dir"]) / "events.jsonl").exists())

    def test_quota_in_result(self):
        self.set_quota(snapshot(4.0))
        res = self.run_fake("done")
        self.assertEqual(res["quota"]["used_percent_before"], 4.0)
        self.assertEqual(res["quota"]["window"], "primary")


class Rules(StoreCase):
    def test_invalid_request(self):
        with self.assertRaises(core.HarnessError) as e:
            core.delegate(request(criteria=None), provider="fake")
        self.assertEqual(e.exception.kind, "invalid_request")
        with self.assertRaises(core.HarnessError):
            core.delegate(request(workdir=str(REPO / "no-such-folder")), provider="fake")

    def test_recursion_guard(self):
        os.environ["AGENT_HARNESS_DEPTH"] = "1"
        with self.assertRaises(core.HarnessError) as e:
            core.delegate(request(), provider="fake")
        self.assertIn("inside a delegate", e.exception.message)
        self.assertEqual(store.list_run_dirs(), [])

    def test_provider_gets_depth_marker(self):
        res = self.run_fake("depth", type="research")
        self.assertEqual(res["answer"]["summary"], "AGENT_HARNESS_DEPTH=1")
        self.assertNotIn("AGENT_HARNESS_DEPTH", os.environ)

    def test_request_key_returns_existing_run(self):
        first = self.run_fake("done", request_key="k1")
        again = self.run_fake("done", request_key="k1")
        self.assertEqual(first["run_id"], again["run_id"])
        self.assertEqual(len(store.list_run_dirs()), 1)

    def test_request_key_retries_failed_run(self):
        first = self.run_fake("failed", request_key="k2")
        second = self.run_fake("done", request_key="k2")
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(store.find_by_request_key("k2").name, second["run_id"])

    def test_thresholds_announced_once_per_window(self):
        self.set_quota(snapshot(52.0))
        first = self.run_fake("done")
        self.assertEqual(sum("quota crossed" in w for w in first["warnings"]), 2)  # before the run; not again after
        self.assertTrue(any("crossed 25%" in w for w in first["warnings"]))
        self.assertTrue(any("crossed 50%" in w for w in first["warnings"]))
        second = self.run_fake("done")
        self.assertFalse(any("quota crossed" in w for w in second["warnings"]))
        self.set_quota(snapshot(76.0))
        third = self.run_fake("done")
        self.assertEqual([w for w in third["warnings"] if "quota crossed" in w][0][:18], "quota crossed 75%:")
        self.set_quota(snapshot(30.0, resets="2026-12-09T01:53:38-05:00"))  # new window
        fourth = self.run_fake("done")
        self.assertTrue(any("crossed 25%" in w for w in fourth["warnings"]))
        self.assertEqual(len(quota.recent_events(limit=50)), 4)

    def test_truncation_keeps_highest_severity(self):
        res = self.run_fake("done")
        rdir = Path(res["run_dir"])
        stored = store.read_json(rdir / "result.json")
        finding = stored["answer"]["findings"][0]
        stored["answer"]["findings"] = ([dict(finding, severity="low", claim="L" * 3000)] * 10
                                        + [dict(finding, severity="high", claim="H")])
        store.write_json(rdir / "result.json", stored)
        cfg = dict(store.DEFAULT_CONFIG, inline_result_chars=12000)
        with mock.patch.object(store, "config", return_value=cfg):
            inline = core.result(res["run_id"])
        self.assertTrue(inline["truncated"])
        self.assertEqual(inline["answer"]["findings"][0]["claim"], "H")
        self.assertLess(len(inline["answer"]["findings"]), 11)
        self.assertEqual(len(core.result(res["run_id"], full=True)["answer"]["findings"]), 11)


class Locking(StoreCase):
    def test_stale_lock_taken_over(self):
        rdir = store.new_run_dir("x")
        store.write_json(rdir / "lock", {"pid": dead_pid(), "role": "worker"})
        info = store.acquire(rdir, "test")
        self.assertEqual(info["pid"], os.getpid())
        store.release(rdir)
        self.assertFalse((rdir / "lock").exists())

    def test_live_lock_refused(self):
        rdir = store.new_run_dir("x")
        store.acquire(rdir, "test")
        with self.assertRaises(store.Locked):
            store.acquire(rdir, "other")

    def test_running_with_dead_worker_becomes_interrupted(self):
        res = self.run_fake("done")
        rdir = Path(res["run_dir"])
        stored = store.read_json(rdir / "result.json")
        stored.update(state="running", answer=None, error=None)
        stored["timing"].update(finished=None, seconds=None)
        store.write_json(rdir / "result.json", stored)
        store.write_json(rdir / "lock", {"pid": dead_pid(), "role": "worker"})
        after = core.result(res["run_id"])
        self.assertEqual((after["state"], after["error"]["kind"]), ("failed", "interrupted"))
        self.assertEqual(schema.validate_result(after), [])
        self.assertFalse((rdir / "lock").exists())

    def test_label_while_worker_holds_lock_and_reply_refused(self):
        res = self.run_fake("blocked")
        rdir = Path(res["run_dir"])
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            store.write_json(rdir / "lock", {"pid": holder.pid, "role": "worker"})
            labelled = core.label(res["run_id"], "useful", note="while locked")
            self.assertEqual(labelled["label"]["outcome"], "useful")
            with self.assertRaises(core.HarnessError) as e:
                core.reply(res["run_id"], "answer")
            self.assertIn("busy", e.exception.message)
        finally:
            holder.kill()
            holder.wait()
        self.assertEqual(store.read_json(rdir / "meta.json")["turns"][-1]["state"], "blocked")

    def test_label_validation(self):
        res = self.run_fake("done")
        with self.assertRaises(core.HarnessError):
            core.label(res["run_id"], "great")
        with self.assertRaises(core.HarnessError):
            core.label(res["run_id"], "partial", mast="other")
        out = core.label(res["run_id"], "partial", mast="verification", note="n")
        self.assertEqual(out["label"]["mast"], "verification")
        self.assertEqual(schema.validate_result(out), [])


class Reply(StoreCase):
    def test_blocked_then_done(self):
        res = self.run_fake("blocked_then_done")
        self.assertEqual(res["state"], "blocked")
        out = core.reply(res["run_id"], "Use the v0 behaviour.")
        self.assertEqual(out["state"], "done")
        rdir = Path(out["run_dir"])
        for name in ("reply-2.md", "events-2.jsonl", "result-2.md"):
            self.assertTrue((rdir / name).exists(), name)
        self.assertEqual(out["usage"]["input_tokens"], 89823 + res["usage"]["input_tokens"])
        self.assertEqual(len(store.read_json(rdir / "meta.json")["turns"]), 2)

    def test_reply_needs_blocked_run(self):
        res = self.run_fake("done")
        with self.assertRaises(core.HarnessError) as e:
            core.reply(res["run_id"], "x")
        self.assertIn("not blocked", e.exception.message)


class Report(StoreCase):
    def test_report_lists_runs_and_questions(self):
        self.run_fake("done")
        blocked = self.run_fake("blocked")
        rep = core.report(workdir=str(REPO))
        self.assertEqual(rep["totals"]["runs"], 2)
        self.assertEqual(rep["pending_questions"][0]["run_id"], blocked["run_id"])
        self.assertEqual(rep["runs"][0]["run_id"], blocked["run_id"])
        self.assertEqual(core.report(workdir=str(REPO / "harness"))["totals"]["runs"], 0)


class DetachedWorker(StoreCase):
    inline = False

    def test_spawned_worker_completes_run(self):
        progress = []
        res = core.delegate(request(model="fake:slow"), provider="fake",
                            progress=lambda m, s: progress.append(m))
        self.assertEqual(res["state"], "done")
        self.assertIn(res["run_id"], progress[0])
        meta = store.read_json(Path(res["run_dir"]) / "meta.json")
        # Breakaway depends on the caller's job object (some sandboxes refuse it); a refusal must be reported.
        self.assertIn(meta["breakaway"], (True, False, None))
        if meta["breakaway"] is False:
            self.assertTrue(any("job object" in w for w in res["warnings"]))
        self.assertFalse((Path(res["run_dir"]) / "lock").exists())

    def test_detach_returns_running_then_finishes(self):
        res = core.delegate(request(model="fake:slow"), provider="fake", wait=False)
        self.assertEqual(res["state"], "running")
        self.assertEqual(schema.validate_result(res), [])
        deadline = time.monotonic() + 60
        while core.result(res["run_id"])["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.5)
        self.assertEqual(core.result(res["run_id"])["state"], "done")


if __name__ == "__main__":
    unittest.main()
