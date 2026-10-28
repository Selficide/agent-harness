import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import adapters, paths, redact, store, worker
from tests.test_core import StoreCase


class Outside(unittest.TestCase):
    """A home folder holding a work folder (work/notes.md) and a secret (.ssh/id_rsa, 'My Docs/key.txt')."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        self.work = self.home / "work"
        for name in ("work/notes.md", ".ssh/id_rsa", "My Docs/key.txt", "shared/spec.md"):
            (self.home / name).parent.mkdir(parents=True, exist_ok=True)
            (self.home / name).write_text("x\n", encoding="utf-8")
        self.patch = mock.patch.object(Path, "home", return_value=self.home)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def found(self, *commands, inputs=("notes.md",), allowed=()):
        return paths.outside_paths({"workdir": str(self.work), "inputs": list(inputs)}, commands, allowed)

    def test_inside_workdir_and_missing_paths_are_quiet(self):
        self.assertEqual(self.found(f"rg -n x notes.md {self.work / 'notes.md'}", "rg '/api/v1' notes.md",
                                    f"cat {self.home / 'nothing-here.txt'}"), [])

    def test_absolute_home_and_escape_reads_are_found(self):
        key = str(self.home / ".ssh" / "id_rsa")
        self.assertEqual(self.found(f"Get-Content -Raw {key}"), [key])
        self.assertEqual(self.found("cat ~/.ssh/id_rsa"), [key])
        self.assertEqual(self.found("Get-Content $env:USERPROFILE\\.ssh\\id_rsa"), [key])
        self.assertEqual(self.found("cat ../.ssh/id_rsa"), [key])

    def test_codex_doubled_backslashes_and_spaces(self):
        doubled = str(self.home / "My Docs" / "key.txt").replace("\\", "\\\\")
        cmd = f'"C:\\\\WINDOWS\\\\powershell.exe" -Command \'Get-Content -Encoding UTF8 {doubled} -TotalCount 5\''
        self.assertEqual(self.found(cmd), [str(self.home / "My Docs" / "key.txt")])

    def test_request_paths_allowed_roots_and_the_executable_are_not_counted(self):
        spec, exe = self.home / "shared" / "spec.md", self.home / ".ssh" / "id_rsa"
        self.assertEqual(self.found(f"cat {spec}", inputs=["notes.md", f"{spec} (the spec)"]), [])
        # The named file's folder may be listed; its other files are still outside.
        docs = self.home / "My Docs"
        self.assertEqual(self.found(f"ls '{docs}'", f"cat '{docs / 'key.txt'}'", inputs=[str(docs / "other.md")]),
                         [str(docs / "key.txt")])
        self.assertEqual(self.found(f"cat {spec}", allowed=[self.home / "shared"]), [])
        self.assertEqual(self.found(f'"{exe}" -n x notes.md'), [])

    def test_warning(self):
        self.assertIsNone(paths.outside_warning([]))
        self.assertIn("named 1 path(s) outside", paths.outside_warning(["/x"]))


class AgentsFiles(unittest.TestCase):
    def test_from_workdir_up_to_the_git_root(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            sub = root / "repo" / "pkg"
            sub.mkdir(parents=True)
            (root / "repo" / ".git").mkdir()
            for f in (root / "AGENTS.md", root / "repo" / "AGENTS.md", sub / "AGENTS.override.md"):
                f.write_text("x", encoding="utf-8")
            self.assertEqual(paths.instruction_files(sub), [str(sub / "AGENTS.override.md"),
                                                       str(root / "repo" / "AGENTS.md")])
            (root / "repo" / ".git").rmdir()
            self.assertEqual(paths.instruction_files(sub), [str(sub / "AGENTS.override.md")])


class Redact(unittest.TestCase):
    def test_kinds(self):
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl"
        cases = {
            "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n-----END OPENSSH PRIVATE KEY-----": "private_key",
            jwt: "jwt", "sk-proj-abcdefghijklmnopqrstuvwx": "api_key", "ghp_" + "a" * 36: "github_token",
            "AKIAABCDEFGHIJKLMNOP": "aws_key", "AIza" + "b" * 35: "google_key", "xoxb-1234567890-abc": "slack_token",
        }
        for secret, kind in cases.items():
            out, counts = redact.text(f"before {secret} after")
            self.assertEqual((out, counts), (f"before [REDACTED:{kind}] after", {kind: 1}), kind)

    def test_assignments_keep_the_name(self):
        out, counts = redact.text('"refresh_token": "rt_abcdefghijklmnop", password=hunter2hunter2 api_key: short')
        self.assertEqual(out, '"refresh_token": "[REDACTED:assignment]", password=[REDACTED:assignment] api_key: short')
        self.assertEqual(counts, {"assignment": 2})

    def test_ordinary_text_is_untouched(self):
        text = "The wiki says 4.9 m (95%); see raw/a.md:3. Passwords must be 12+ characters. sk-short"
        self.assertEqual(redact.text(text), (text, {}))

    def test_json_lines_stay_valid(self):
        line = json.dumps({"item": {"aggregated_output": '{"tokens": {"access_token": "' + "q" * 20 + '\\"x"}}\n'
                                    "-----BEGIN RSA PRIVATE KEY-----\nMIIabc+/=\n", "id": 1}})
        out, counts = redact.text(line)
        self.assertEqual(json.loads(out)["item"]["id"], 1)
        self.assertEqual(counts, {"assignment": 1, "private_key": 1})

    def test_value_walks_json(self):
        out, counts = redact.value({"findings": [{"evidence": "key sk-" + "c" * 24}], "n": 3})
        self.assertEqual(out, {"findings": [{"evidence": "key [REDACTED:api_key]"}], "n": 3})
        self.assertEqual(counts, {"api_key": 1})


class Worker(StoreCase):
    def test_secret_in_answer_is_redacted_everywhere(self):
        res = self.run_fake("done")
        rdir = Path(res["run_dir"])
        answer = json.loads((rdir / "result.md").read_text(encoding="utf-8"))
        answer["findings"][0]["evidence"] = "auth.json has sk-proj-" + "d" * 30
        (rdir / "result.md").write_text(json.dumps(answer), encoding="utf-8")
        meta = store.read_json(rdir / "meta.json")
        meta["turns"][-1]["warnings"] = []
        store.write_json(rdir / "meta.json", meta)
        with mock.patch("harness.worker.adapters.get", return_value=_NoRun(adapters.get("fake"))):
            worker.execute(rdir, 1)
        res = store.read_json(rdir / "result.json")
        self.assertIn("[REDACTED:api_key]", res["answer"]["findings"][0]["evidence"])
        self.assertNotIn("sk-proj-d", (rdir / "result.md").read_text(encoding="utf-8"))
        self.assertTrue(any(w.startswith("redacted: ") for w in res["warnings"]), res["warnings"])

    def test_agents_file_warning(self):
        res = self.run_fake("done")
        meta = store.read_json(Path(res["run_dir"]) / "meta.json")
        meta["agents_files"] = ["/x/AGENTS.md"]
        built = worker.build_result(Path(res["run_dir"]), meta, "done", None, None)
        self.assertTrue(any("/x/AGENTS.md" in w for w in built["warnings"]))

    def test_warnings_repeated_by_a_reply_turn_appear_once(self):
        res = self.run_fake("done")
        meta = store.read_json(Path(res["run_dir"]) / "meta.json")
        meta["turns"] = [{"turn": 1, "warnings": ["outside workdir: x"]}, {"turn": 2, "warnings": ["outside workdir: x"]}]
        built = worker.build_result(Path(res["run_dir"]), meta, "done", None, None)
        self.assertEqual(built["warnings"].count("outside workdir: x"), 1)


class _NoRun:
    """The fake adapter with a command that does nothing, so execute() re-reads the files already there."""

    def __init__(self, real):
        self.real = real

    def __getattr__(self, name):
        return getattr(self.real, name)

    def command(self, run, turn):
        import sys
        return [sys.executable, "-c", "import sys; sys.stdin.read()"], run["workdir"]


if __name__ == "__main__":
    unittest.main()
