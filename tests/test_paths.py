import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness import cli, core, paths, store, worker
from harness.adapters import codex as codex_adapter
from harness.prompts import codex as prompts
from tests.test_core import FIXTURES, StoreCase, request


class Folder(unittest.TestCase):
    """A workdir with raw/a.md, raw/b.md, raw/data.md, wiki/What's New.md and an empty folder docs/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for name in ("raw/a.md", "raw/b.md", "raw/data.md", "wiki/What's New.md"):
            (self.dir / name).parent.mkdir(parents=True, exist_ok=True)
            (self.dir / name).write_text("x\n", encoding="utf-8")
        (self.dir / "docs").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def req(self, inputs, source_of_truth=None):
        r = {"workdir": str(self.dir), "inputs": inputs}
        if source_of_truth is not None:
            r["source_of_truth"] = source_of_truth
        return r


class PathPart(unittest.TestCase):
    def test_paths(self):
        for item, want in [("quota.py", "quota.py"), ("raw/a.md", "raw/a.md"),
                           ("wiki/sources/GPS.gov - GPS Accuracy.md", "wiki/sources/GPS.gov - GPS Accuracy.md"),
                           ("meta/Engine.md (sections 'Tools (tools/)' and 'Layers')", "meta/Engine.md"),
                           ("C:/x/txt (full texts: *-html.md)", "C:/x/txt"), ("/tmp/a b/c", "/tmp/a b/c"),
                           (r"raw\*.md", r"raw\*.md")]:
            self.assertEqual(paths.path_part(item), want, item)

    def test_phrases(self):
        for item in ["intended behaviour", "The module docstring of harness/template.py",
                     "The docstrings in quota.py", "Other raw/articles/*.md files cited by the pages",
                     "https://example.com/a.md", "v1.2 of the spec (draft)"]:
            self.assertIsNone(paths.path_part(item), item)


class Resolve(Folder):
    def test_files_folders_phrases_and_urls_pass(self):
        r = self.req(["raw/a.md", "docs", str(self.dir / "raw/b.md"), "https://example.com/x.html"],
                     ["the code's own intended behaviour", "intended behaviour"])
        out, patterns, problems = paths.resolve(r)
        self.assertEqual((out, patterns, problems), (r, {}, []))

    def test_missing_path_is_a_problem(self):
        _, _, problems = paths.resolve(self.req(["raw/a.md", "raw/missing.md"], ["spec/none"]))
        self.assertEqual(problems, ["inputs: no such file or folder: raw/missing.md",
                                    "source_of_truth: no such file or folder: spec/none"])

    def test_pattern_expands_to_sorted_files(self):
        out, patterns, problems = paths.resolve(self.req(["wiki/What's New.md"], ["raw/*.md"]))
        self.assertEqual(problems, [])
        self.assertEqual(out["source_of_truth"], ["raw/a.md", "raw/b.md", "raw/data.md"])
        self.assertEqual(patterns, {"raw/*.md": 3})

    def test_notes_after_paths_are_kept(self):
        r = self.req(["raw/a.md (the first part)"], ["raw/*.md (the raw files)"])
        out, patterns, problems = paths.resolve(r)
        self.assertEqual((out, patterns, problems), (r, {"raw/*.md (the raw files)": 3}, []))
        _, _, problems = paths.resolve(self.req(["raw/none.md (gone)"]))
        self.assertEqual(problems, ["inputs: no such file or folder: raw/none.md"])

    def test_pattern_with_no_match_or_too_many(self):
        _, _, problems = paths.resolve(self.req(["raw/*.txt"]))
        self.assertEqual(problems, ["inputs: pattern matches no files: raw/*.txt"])
        old = paths.MAX_PATTERN_MATCHES
        paths.MAX_PATTERN_MATCHES = 2
        try:
            _, _, problems = paths.resolve(self.req(["raw/*.md"]))
        finally:
            paths.MAX_PATTERN_MATCHES = old
        self.assertIn("more than 2", problems[0])


class Coverage(Folder):
    def test_named_in_a_command_or_files_read(self):
        r = self.req(["raw/a.md", "wiki/What's New.md", "docs", "a phrase"], ["raw/b.md", "raw/data.md"])
        commands = [r"powershell.exe -Command 'Get-Content raw\a.md; Get-Content ''wiki/What''s New.md'''"]
        unreached, total = paths.coverage(r, commands, ["./raw/b.md"])
        self.assertEqual((unreached, total), (["raw/data.md"], 4))

    def test_a_longer_name_does_not_count(self):
        unreached, _ = paths.coverage(self.req(["raw/a.md"], ["raw/data.md"]), ["rg x raw/data.md"], [])
        self.assertEqual(unreached, ["raw/a.md"])

    def test_absolute_path_counts(self):
        unreached, _ = paths.coverage(self.req(["raw/a.md"]), [f"cat {self.dir / 'raw' / 'a.md'}"], [])
        self.assertEqual(unreached, [])

    def test_warning_lists_a_few(self):
        self.assertIsNone(paths.coverage_warning([], 3))
        text = paths.coverage_warning([f"f{i}" for i in range(7)], 9)
        self.assertIn("7 of 9 files", text)
        self.assertIn("f4 and 2 more", text)


class Garbled(unittest.TestCase):
    def finding(self, claim, evidence="line 3"):
        return {"severity": "low", "location": "a.md:3", "claim": claim, "evidence": evidence, "confidence": "high"}

    def test_flags_mojibake_and_encoding_claims(self):
        answer = {"findings": [self.finding("Unit is corrupted", "~3 kmÂ² here"),
                               self.finding("The page says 4 m but the source says 5 m"),
                               self.finding("Corrupted unit symbol in the accuracy row"),
                               self.finding("Dash shows as â€” in the summary"),
                               self.finding("Fraction appears as �")]}
        self.assertIn("findings 1, 3, 4, 5 ", worker.garbled_warning(answer))

    def test_valid_non_ascii_is_not_flagged(self):
        answer = {"findings": [self.finding("Château says ±3 m² — not 5 m", "“⅔ of ideas”, café, naïve")]}
        self.assertIsNone(worker.garbled_warning(answer))


class Parse(unittest.TestCase):
    def test_collects_shell_commands(self):
        parsed = codex_adapter.parse_turn(FIXTURES / "20261009-160446-review-codex-delegate-py-for-correctness", 1)
        self.assertEqual(len(parsed["commands"]), 8)  # item.completed only, not item.started
        self.assertTrue(all(isinstance(c, str) and c for c in parsed["commands"]))


class Prompt(unittest.TestCase):
    def test_encoding_note_only_on_windows(self):
        r = {"type": "research", "task": "t", "objective": "o", "inputs": ["a.md"]}
        text = prompts.render(r)
        self.assertEqual(prompts.WINDOWS_ENCODING in text, __import__("os").name == "nt")


class Cli(StoreCase):
    def test_prints_non_ascii_to_an_ansi_stdout(self):
        res = self.run_fake("done")
        core.label(res["run_id"], "useful", note="⅔ of fixes")
        out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with mock.patch("sys.stdout", out):
            self.assertEqual(cli.main(["result", res["run_id"]]), 0)
            out.flush()
            self.assertIn("⅔ of fixes", out.buffer.getvalue().decode("utf-8"))


class Delegate(StoreCase):
    def test_missing_input_is_refused_before_a_run(self):
        with self.assertRaises(core.HarnessError) as cm:
            core.delegate(request(inputs=["README.md", "no/such-file.md"]), provider="fake")
        self.assertEqual(cm.exception.kind, "invalid_request")
        self.assertIn("no/such-file.md", cm.exception.message)
        self.assertEqual(list(store.list_run_dirs()), [])

    def test_pattern_is_expanded_and_recorded(self):
        res = self.run_fake("done", inputs=["harness/adapters/*.py"])
        rdir = Path(res["run_dir"])
        sent = store.read_json(rdir / "request.json")["inputs"]
        self.assertIn("harness/adapters/codex.py", sent)
        self.assertEqual(store.read_json(rdir / "meta.json")["patterns"], {"harness/adapters/*.py": len(sent)})

    def test_coverage_warning_on_unread_inputs(self):
        # The replayed run read codex_delegate.py and schemas, never README.md or CONTRACT.md.
        res = self.run_fake("done", inputs=["README.md", "CONTRACT.md"])
        self.assertTrue(any(w.startswith("coverage: 2 of 2 files") for w in res["warnings"]), res["warnings"])
        meta = store.read_json(Path(res["run_dir"]) / "meta.json")
        self.assertEqual(meta["turns"][-1]["coverage"], {"unreached": ["README.md", "CONTRACT.md"], "total": 2})


if __name__ == "__main__":
    unittest.main()
