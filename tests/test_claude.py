import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import adapters, core, paths, schema, store
from harness.adapters import claude
from tests.test_core import REPO, StoreCase, request

ANSWER = {"status": "done", "summary": "The budget is 17 of 20.", "questions": [], "files_read": ["README.md"],
          "findings": [{"claim": "17 of 20", "evidence": "README.md:2", "confidence": "high"}]}


def events(answer=ANSWER, tools=(("Read", {"file_path": str(REPO / "README.md")}),), error=None):
    """A stream-json transcript like `claude -p --output-format stream-json --verbose` writes."""
    lines = [{"type": "system", "subtype": "init", "session_id": "sess-1", "model": "claude-haiku-5-5",
              "tools": ["Glob", "Grep", "Read", "StructuredOutput"]}]
    for i, (name, args) in enumerate(tools):
        lines.append({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Looking."}, {"type": "tool_use", "id": f"t{i}", "name": name, "input": args}]}})
        lines.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                                               "content": "ok"}]}})
    lines.append({"type": "result", "subtype": "success", "is_error": bool(error), "result": error or "",
                  "session_id": "sess-1", "total_cost_usd": 0.0123, "permission_denials": [],
                  "structured_output": None if error else answer,
                  "usage": {"input_tokens": 100, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 400,
                            "output_tokens": 30, "output_tokens_details": {"thinking_tokens": 12}}})
    return "\n".join(json.dumps(x) for x in lines) + "\n"


class Parse(unittest.TestCase):
    def test_stream_json(self):
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d)
            (rdir / "events.jsonl").write_text(events(tools=[("Read", {"file_path": "a.md"}),
                                                             ("Grep", {"pattern": "x", "path": "src"}),
                                                             ("Glob", {"pattern": "**/*.md"}),
                                                             ("StructuredOutput", {"summary": "~/.ssh"})]),
                                               encoding="utf-8")
            out = claude.parse_turn(rdir, 1)
            self.assertEqual(out["thread_id"], "sess-1")
            self.assertEqual(out["commands"], ["Read a.md", "Grep src x", "Glob **/*.md"])
            self.assertEqual(out["usage"], {"input_tokens": 550, "cached_input_tokens": 400, "output_tokens": 30,
                                            "reasoning_tokens": 12})
            self.assertEqual(out["cost_usd"], 0.0123)
            self.assertEqual(json.loads(out["answer_text"]), ANSWER)
            # The answer is written where a Codex answer would be, and that file wins afterwards (redaction edits it).
            self.assertEqual(json.loads((rdir / "result.md").read_text(encoding="utf-8")), ANSWER)

    def test_error(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "events.jsonl").write_text(events(error="Failed to authenticate"), encoding="utf-8")
            out = claude.parse_turn(Path(d), 1)
            self.assertEqual((out["answer_text"], out["errors"]), (None, ["Failed to authenticate"]))


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rdir = Path(self.tmp.name)
        self.outside = Path(tempfile.mkdtemp()).resolve()
        (self.outside / "spec.md").write_text("x", encoding="utf-8")
        store.write_json(self.rdir / "request.json", request(type="research", inputs=["README.md",
                                                                                     str(self.outside / "spec.md")]))
        self.run = {"run_dir": str(self.rdir), "workdir": str(REPO), "type": "research", "model": "claude-haiku-5-5",
                    "effort": "medium", "thread_id": "sess-1"}
        self.patch = mock.patch.object(claude.ClaudeAdapter, "exe", return_value="claude")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_read_only_flags(self):
        argv, cwd = claude.ClaudeAdapter().command(self.run, 1)
        self.assertEqual(cwd, str(REPO))
        for flags in (["--restricted"], ["--tools", "Read,Grep,Glob"], ["--strict-mcp-config"],
                      ["--permission-mode", "dontAsk"], ["--model", "claude-haiku-5-5"],
                      ["--add-dir", str(self.outside)], ["--max-budget-usd", "1.0"]):
            i = argv.index(flags[0])
            self.assertEqual(argv[i:i + len(flags)], flags)
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1])["required"][0], "status")
        self.assertNotIn("--resume", argv)

    def test_reply_resumes_the_session(self):
        argv, _ = claude.ClaudeAdapter().command(self.run, 2)
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-1")

    def test_env_drops_the_parent_session(self):
        env = claude.ClaudeAdapter().env({"CLAUDECODE": "1", "CLAUDE_CODE_SIMPLE": "1", "CLAUDE_CODE_SESSION_ID": "x",
                                          "AI_AGENT": "x", "CLAUDE_CODE_OAUTH_TOKEN": "t", "PATH": "p",
                                          "AGENT_HARNESS_DEPTH": "1"})
        self.assertEqual(env, {"CLAUDE_CODE_OAUTH_TOKEN": "t", "PATH": "p", "AGENT_HARNESS_DEPTH": "1"})


class Config(StoreCase):
    def test_partial_dict_override_keeps_other_defaults(self):
        store.write_json(store.home() / "config.json", {"default_model": {"codex": "x"}, "default_timeout_s": 60})
        cfg = store.config()
        self.assertEqual(cfg["default_model"], {"codex": "x", "claude": "claude-haiku-5-5"})
        self.assertEqual((cfg["default_timeout_s"], cfg["default_effort"]["research"]), (60, "medium"))
        self.assertEqual(store.DEFAULT_CONFIG["default_model"]["codex"], "gpt-6-luna")


class InstructionFiles(unittest.TestCase):
    def test_claude_reads_above_the_git_root(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            sub = root / "repo"
            (sub / ".git").mkdir(parents=True)
            (root / "CLAUDE.md").write_text("x", encoding="utf-8")
            (sub / "CLAUDE.local.md").write_text("x", encoding="utf-8")
            names = claude.ClaudeAdapter.instruction_files
            self.assertEqual(paths.instruction_files(sub, names), [str(sub / "CLAUDE.local.md")])
            self.assertEqual(paths.instruction_files(sub, names, up_to_git_root=False),
                             [str(sub / "CLAUDE.local.md"), str(root / "CLAUDE.md")])


class Delegate(StoreCase):
    def test_review_is_refused(self):
        with self.assertRaises(core.HarnessError) as e:
            core.delegate(request(provider="claude", model="claude-haiku-5-5"))
        self.assertEqual(e.exception.kind, "invalid_request")
        self.assertIn("research", e.exception.message)

    def test_research_run_end_to_end(self):
        script = Path(self.tmp.name) / "events.txt"
        script.write_text(events(tools=[("Read", {"file_path": str(REPO / "README.md")}),
                                        ("Read", {"file_path": str(Path.home() / ".gitconfig")})]), encoding="utf-8")

        class Scripted(claude.ClaudeAdapter):
            def exe(self):
                return sys.executable

            def command(self, run, turn):
                return [sys.executable, "-c", f"import sys; sys.stdin.read(); "
                                              f"sys.stdout.write(open({str(script)!r}, encoding='utf-8').read())"], \
                    run["workdir"]

        with mock.patch.object(adapters, "get", lambda name: Scripted()):
            res = core.delegate({"workdir": str(REPO), "type": "research", "task": "Test research",
                                 "objective": "Find the budget.", "inputs": ["README.md"], "provider": "claude"})
        self.assertEqual(schema.validate_result(res, "research"), [], res)
        self.assertEqual((res["state"], res["provider"], res["prompt_version"]), ("done", "claude", "claude-1"))
        self.assertEqual(res["answer"]["summary"], ANSWER["summary"])
        self.assertEqual(res["usage"]["output_tokens"], 30)
        meta = store.read_json(Path(res["run_dir"]) / "meta.json")
        self.assertEqual(meta["turns"][-1]["cost_usd"], 0.0123)
        self.assertEqual(meta["turns"][-1]["coverage"], {"unreached": [], "total": 1})
        if (Path.home() / ".gitconfig").exists():
            self.assertTrue(any(w.startswith("outside workdir") for w in res["warnings"]), res["warnings"])


if __name__ == "__main__":
    unittest.main()
