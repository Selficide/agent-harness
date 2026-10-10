import contextlib
import hashlib
import io
import json
import os
from pathlib import Path

from harness import cli, core, schema, store, v0

from .test_core import REPO, StoreCase, request

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOURCES = (FIXTURES / "v0-runs", FIXTURES / "v0-constructed")
LABELLED = "20261009-160446-review-codex-delegate-py-for-correctness"
BLOCKED = "20261009-155826-review-codex-delegate-py-for-correctness"
RESEARCH = "20261009-161031-list-the-subcommands-of-codex-delegate-p"
UNLABELLED = "20261009-161031-unlabelled-research"
TWO_TURN = "20261009-155826-two-turn-review"


def digests(*roots):
    out = {}
    for root in roots:
        for f in sorted(Path(root).rglob("*")):
            if f.is_file():
                out[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def v0_totals(run_dirs):
    """What the retired codex_delegate.py `report` computed: token sums and outcome counts from meta.json."""
    tin = tout = 0
    outcomes = {}
    for d in run_dirs:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        tin += sum(t["usage"].get("input_tokens", 0) for t in meta["turns"])
        tout += sum(t["usage"].get("output_tokens", 0) for t in meta["turns"])
        outcome = (meta.get("label") or {}).get("outcome", "unlabelled")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return tin, tout, outcomes


class Migration(StoreCase):
    def setUp(self):
        super().setUp()
        self.before = digests(*SOURCES)
        for src in SOURCES:
            v0.migrate(src)

    def tearDown(self):
        self.assertEqual(digests(*SOURCES), self.before, "fixtures were modified")
        super().tearDown()

    def result(self, run_id):
        res = core.result(run_id, full=True)
        self.assertEqual(schema.validate_result(res), [], run_id)
        return res

    def test_copies_are_byte_identical(self):
        for src in SOURCES:
            for d in src.iterdir():
                self.assertEqual(digests(d).values().__len__(), digests(store.runs_dir() / d.name).values().__len__())
                self.assertEqual(sorted(digests(d).values()), sorted(digests(store.runs_dir() / d.name).values()))
        copied, skipped = v0.migrate(SOURCES[0])
        self.assertEqual((copied, len(skipped)), ([], 3))

    def test_labelled_single_turn(self):
        res = self.result(LABELLED)
        self.assertEqual((res["state"], res["prompt_version"], res["provider"]), ("done", "v0", "codex"))
        self.assertEqual(res["label"]["outcome"], "useful")
        self.assertRegex(res["label"]["labelled"], r"[+-]\d\d:\d\d$")
        self.assertEqual(res["usage"]["input_tokens"], 89823)
        self.assertEqual(res["usage"]["reasoning_tokens"], 1957)
        self.assertIsNone(res["quota"])  # before plan tracking existed
        self.assertEqual(len(res["answer"]["findings"]), 1)

    def test_blocked_and_useless(self):
        res = self.result(BLOCKED)
        self.assertEqual(res["state"], "blocked")
        self.assertEqual(res["label"]["outcome"], "useless")

    def test_unlabelled_with_plan_usage(self):
        res = self.result(UNLABELLED)
        self.assertIsNone(res["label"])
        self.assertEqual((res["quota"]["used_percent_before"], res["quota"]["used_percent_after"]), (1.0, 1.0))
        self.assertEqual(res["quota"]["window"], "primary")
        self.assertRegex(res["quota"]["resets_at"], r"[+-]\d\d:\d\d$")
        self.assertEqual(schema.validate_result(res, "research"), [])

    def test_two_turn_run(self):
        res = self.result(TWO_TURN)
        self.assertEqual(res["state"], "done")  # from result-2.json, the last turn
        self.assertEqual(len(res["answer"]["findings"]), 1)
        meta = store.read_json(store.run_dir(TWO_TURN) / "meta.json")
        self.assertEqual(res["usage"]["input_tokens"], sum(t["usage"]["input_tokens"] for t in meta["turns"]))
        self.assertEqual((res["quota"]["used_percent_before"], res["quota"]["used_percent_after"]), (1.0, 2.0))
        self.assertEqual(res["timing"]["seconds"], round(sum(t["seconds"] for t in meta["turns"]), 1))

    def test_label_v0_run_writes_label_file_only(self):
        meta_path = store.run_dir(UNLABELLED) / "meta.json"
        before = meta_path.read_bytes()
        res = core.label(UNLABELLED, "partial", "verification", "relabelled")
        self.assertEqual(res["label"]["outcome"], "partial")
        self.assertEqual(meta_path.read_bytes(), before)
        self.assertEqual(core.label(LABELLED, "partial")["label"]["outcome"], "partial")  # label.json wins

    def test_reply_to_v0_refused(self):
        with self.assertRaises(core.HarnessError) as e:
            core.reply(BLOCKED, "answer")
        self.assertIn("v0", e.exception.message)
        self.assertFalse((store.run_dir(BLOCKED) / "lock").exists())

    def test_report_lists_v0_and_v1_with_v0_totals(self):
        new = core.delegate(request(), provider="fake")
        rep = core.report()
        ids = [r["run_id"] for r in rep["runs"]]
        self.assertEqual(ids[0], new["run_id"])
        self.assertEqual(rep["totals"]["runs"], 6)
        tin, tout, outcomes = v0_totals([d for src in SOURCES for d in src.iterdir()])
        self.assertEqual(rep["totals"]["input_tokens"], tin + new["usage"]["input_tokens"])
        self.assertEqual(rep["totals"]["output_tokens"], tout + new["usage"]["output_tokens"])
        outcomes["unlabelled"] = outcomes.get("unlabelled", 0) + 1
        self.assertEqual(rep["totals"]["outcomes"], outcomes)
        self.assertEqual(rep["pending_questions"], [])  # v0's blocked run can't be replied to


class Cli(StoreCase):
    def run_cli(self, *argv, stdin=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_delegate_result_label_report(self):
        req = Path(self.tmp.name) / "req.json"
        req.write_text(json.dumps(request()), encoding="utf-8")
        code, out = self.run_cli("delegate", str(req), "--provider", "fake")
        res = json.loads(out)
        self.assertEqual((code, res["state"]), (0, "done"))
        code, out = self.run_cli("result", res["run_id"])
        self.assertEqual(json.loads(out)["run_id"], res["run_id"])
        code, out = self.run_cli("label", res["run_id"], "--outcome", "useful", "--note", "ok")
        self.assertEqual(json.loads(out)["label"]["note"], "ok")
        code, out = self.run_cli("report", "--workdir", str(REPO))
        self.assertIn("1 runs |", out)
        self.assertIn("useful=1", out)
        code, out = self.run_cli("report", "--json")
        self.assertEqual(json.loads(out)["totals"]["runs"], 1)

    def test_invalid_request_exits_2(self):
        req = Path(self.tmp.name) / "req.json"
        req.write_text(json.dumps(request(type="execute")), encoding="utf-8")
        code, out = self.run_cli("delegate", str(req), "--provider", "fake")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"]["kind"], "invalid_request")
        code, out = self.run_cli("result", "no-such-run")
        self.assertEqual(code, 2)

    def test_migrate_command(self):
        code, out = self.run_cli("migrate-v0", str(SOURCES[0]))
        self.assertIn("copied 3 v0 run(s)", out)
        self.assertTrue(os.path.isdir(store.run_dir(LABELLED)))
