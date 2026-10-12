import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from harness import cli, core, store, template
from tests.test_core import REPO, StoreCase


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class GitRepoCase(StoreCase):
    """A store plus a git repo with one committed file. The fake provider runs in the repo, so it gets PYTHONPATH."""

    def setUp(self):
        super().setUp()
        self.pythonpath = mock.patch.dict(os.environ, {"PYTHONPATH": str(REPO)})
        self.pythonpath.start()
        self.repo_tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.repo_tmp.name).resolve()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "a.py").write_text("x = 1\n")
        git(self.repo, "add", "a.py")
        git(self.repo, "commit", "-q", "-m", "init")

    def tearDown(self):
        self.pythonpath.stop()
        self.repo_tmp.cleanup()
        super().tearDown()

    def change(self):
        (self.repo / "a.py").write_text("x = 2\n")
        (self.repo / "b.py").write_text("y = 3\n")


class Bundled(unittest.TestCase):
    def test_every_template_is_well_formed(self):
        self.assertIn("diff-review", template.names())
        for name in template.names():
            template.load(name)  # raises if malformed
        self.assertEqual(len(template.describe()), len(template.names()))

    def test_check_catches_bad_shapes(self):
        bad = {"template": "x", "template_version": "1", "vars": {"v": {"type": "dict"}}, "extra": 1,
               "request": {"workdir": "/"}, "captures": {}, "require_any": ["nope"]}
        problems = " ".join(template.check(bad, "x"))
        for word in ("extra", "template_version", "var v", "workdir", "nope"):
            self.assertIn(word, problems)


class Expand(GitRepoCase):
    def test_diff_review_captures_diff_and_untracked(self):
        self.change()
        req, info = template.expand("diff-review", {"spec": "docs/plan.md"}, str(self.repo))
        diff, untracked = Path(req["inputs"][0]), Path(req["inputs"][1])
        self.assertIn("+x = 2", diff.read_text(encoding="utf-8"))
        self.assertEqual(untracked.read_text(encoding="utf-8").split(), ["b.py"])
        self.assertTrue(diff.is_relative_to(store.home() / "captures"))
        self.assertEqual(req["source_of_truth"], ["docs/plan.md"])
        self.assertEqual(req["task"], "Review the diff against HEAD")
        self.assertEqual(req["workdir"], str(self.repo))
        self.assertEqual(len(req["inputs"]), 2)  # context defaults to [] and splices away
        self.assertEqual(info, {"name": "diff-review", "version": 1,
                                "vars": {"base": "HEAD", "spec": ["docs/plan.md"], "context": []}})

    def test_list_var_splices(self):
        self.change()
        req, _ = template.expand("diff-review", {"spec": ["s1", "s2"], "context": ["c1", "c2"]}, str(self.repo))
        self.assertEqual(req["inputs"][2:], ["c1", "c2"])
        self.assertEqual(req["source_of_truth"], ["s1", "s2"])

    def test_same_content_same_capture(self):
        self.change()
        a, _ = template.expand("diff-review", {"spec": "s"}, str(self.repo))
        b, _ = template.expand("diff-review", {"spec": "s"}, str(self.repo))
        self.assertEqual(a["inputs"], b["inputs"])

    def test_nothing_to_review(self):
        with self.assertRaisesRegex(template.TemplateError, "nothing to review"):
            template.expand("diff-review", {"spec": "s"}, str(self.repo))

    def test_untracked_only_is_enough(self):
        (self.repo / "b.py").write_text("y = 3\n")
        req, _ = template.expand("diff-review", {"spec": "s"}, str(self.repo))
        self.assertEqual(Path(req["inputs"][0]).stat().st_size, 0)

    def test_rejections(self):
        self.change()
        cases = [
            ({"spec": "s", "base": "--output=x"}, None, "may not start with '-'"),
            ({}, None, "'spec' is required"),
            ({"spec": "s", "bogus": "1"}, None, "unknown vars"),
            ({"spec": "s", "base": ["a", "b"]}, None, "must be a string"),
            ({"spec": "s"}, {"criteria": ["leak"]}, "only request_key"),
            ({"spec": "s", "base": "no-such-ref"}, None, "exited"),
        ]
        for values, overrides, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(template.TemplateError, message):
                template.expand("diff-review", values, str(self.repo), overrides)
        with self.assertRaisesRegex(template.TemplateError, "no such template"):
            template.expand("nope", {}, str(self.repo))
        with self.assertRaisesRegex(template.TemplateError, "absolute"):
            template.expand("diff-review", {"spec": "s"}, "relative/path")

    def test_overrides_apply(self):
        self.change()
        req, _ = template.expand("diff-review", {"spec": "s"}, str(self.repo), {"effort": "low", "request_key": "k"})
        self.assertEqual((req["effort"], req["request_key"]), ("low", "k"))


class Delegate(GitRepoCase):
    def test_run_records_template(self):
        self.change()
        res = core.delegate_template("diff-review", {"spec": "s"}, str(self.repo), {"model": "fake:done"},
                                     provider="fake")
        self.assertEqual(res["state"], "done")
        meta = store.read_json(Path(res["run_dir"]) / "meta.json")
        self.assertEqual(meta["request_template"]["name"], "diff-review")
        self.assertEqual(core.report()["runs"][0]["template"], "diff-review@1")

    def test_errors_are_invalid_request(self):
        with self.assertRaises(core.HarnessError) as cm:
            core.delegate_template("diff-review", {"spec": "s"}, str(self.repo), {"model": "fake:done"},
                                  provider="fake")
        self.assertEqual(cm.exception.kind, "invalid_request")

    def test_cli_repeated_var_is_a_list(self):
        self.change()
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["delegate", "--template", "diff-review", "--workdir", str(self.repo),
                             "--var", "spec=s1", "--var", "spec=s2", "--model", "fake:done", "--provider", "fake"])
        self.assertEqual(code, 0)
        res = json.loads(out.getvalue())
        self.assertEqual(res["state"], "done")
        request = store.read_json(Path(res["run_dir"]) / "request.json")
        self.assertEqual(request["source_of_truth"], ["s1", "s2"])

    def test_cli_rejects_mixing_file_and_template(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["delegate", "req.json", "--template", "diff-review"]), 2)
            self.assertEqual(cli.main(["delegate", "req.json", "--var", "a=b"]), 2)


if __name__ == "__main__":
    unittest.main()
