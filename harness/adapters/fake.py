"""Fake provider for tests: replays recorded Codex event streams and answers, with scripted behaviour.

The scenario is the request's model, e.g. "fake:done". Scenarios: done, blocked, research, failed, hang, bad_json,
blocked_then_done (blocked on turn 1, done on a reply), slow (done after 3 s), depth (reports
AGENT_HARNESS_DEPTH).
Quota comes from the JSON file named by $AGENT_HARNESS_FAKE_QUOTA (a quota snapshot), if set.

Run as a provider: python -m harness.adapters.fake <scenario> <answer_file> <turn>
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import codex
from .base import Adapter, run_files

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "v0-runs"
RECORDINGS = {
    "done": "20261009-160446-review-codex-delegate-py-for-correctness",
    "blocked": "20261009-155826-review-codex-delegate-py-for-correctness",
    "research": "20261009-161031-list-the-subcommands-of-codex-delegate-p",
}


class FakeAdapter(Adapter):
    name = "fake"
    schema_enforcement = "native"
    prompt_version = "fake-1"

    def defaults(self, cfg):
        return {"model": "fake:done", "effort": cfg["default_effort"]}

    def capabilities(self):
        return {"can_read_files": True, "can_run_commands": False, "can_run_python": False, "network": False}

    def render(self, request):
        return f"# Task: {request['task']}\n\n{request['objective']}\n"

    def command(self, run, turn):
        scenario = run["model"].split(":", 1)[1] if ":" in run["model"] else "done"
        answer = str(Path(run["run_dir"]) / run_files(turn)["answer"])
        return [sys.executable, "-m", "harness.adapters.fake", scenario, answer, str(turn)], run["workdir"]

    def parse(self, run_dir, turn):
        return codex.parse_turn(Path(run_dir), turn)

    def quota_snapshot(self, thread_id=None):
        path = os.environ.get("AGENT_HARNESS_FAKE_QUOTA")
        if not path or not Path(path).exists():
            return None
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def version(self):
        return "fake"

    def doctor(self):
        return [("fake provider", True, str(FIXTURES))]


def _replay(name, answer_file):
    src = FIXTURES / RECORDINGS[name]
    sys.stdout.write((src / "events.jsonl").read_text(encoding="utf-8"))
    sys.stdout.flush()
    sys.stderr.write((src / "stderr.log").read_text(encoding="utf-8", errors="replace"))
    shutil.copyfile(src / "result.md", answer_file)


def main(scenario, answer_file, turn):
    sys.stdin.read()  # consume the prompt like a real provider
    if scenario == "blocked_then_done":
        scenario = "blocked" if turn == 1 else "done"
    if scenario == "slow":
        time.sleep(3)
        scenario = "done"
    if scenario in RECORDINGS:
        _replay(scenario, answer_file)
        return 0
    if scenario == "failed":
        print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}))
        print(json.dumps({"type": "turn.failed", "error": {"message": "model overloaded"}}))
        return 1
    if scenario == "hang":
        time.sleep(600)
        return 0
    if scenario == "depth":  # reports the recursion marker it was given
        answer = {"status": "done", "summary": f"AGENT_HARNESS_DEPTH={os.environ.get('AGENT_HARNESS_DEPTH')}",
                  "findings": [], "questions": [], "files_read": ["x"]}
        Path(answer_file).write_text(json.dumps(answer), encoding="utf-8")
        return 0
    if scenario == "bad_json":
        print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}))
        Path(answer_file).write_text("I looked and it seems fine.", encoding="utf-8")
        return 0
    print(f"unknown fake scenario {scenario!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
