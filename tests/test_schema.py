import copy
import json
import unittest
from pathlib import Path

from harness import schema

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def review_request(**overrides):
    req = {
        "workdir": "D:\\Dev\\vault",
        "type": "review",
        "task": "Review the ingest of the Codex prompting guide",
        "objective": "Find factual errors in the ingested page.",
        "inputs": ["wiki/codex-prompting-guide.md"],
        "criteria": ["Claims that contradict the source"],
        "source_of_truth": ["raw/codex-prompting-guide.html"],
    }
    req.update(overrides)
    return req


def done_result(**overrides):
    res = {
        "contract": "1",
        "run_id": "20261010-120000-review-x",
        "run_dir": "C:\\Users\\me\\.agent-harness\\runs\\20261010-120000-review-x",
        "state": "done",
        "answer": {"status": "done", "summary": "One problem.", "questions": [], "files_read": ["a.md"],
                   "findings": [{"severity": "high", "location": "a.md:3", "claim": "Wrong date.",
                                 "evidence": "src.html:10 says 2025", "confidence": "high"}]},
        "truncated": False,
        "provider": "codex",
        "model": "gpt-6-luna",
        "effort_applied": "high",
        "prompt_version": "codex-review-1",
        "capabilities": {"can_read_files": True, "can_run_commands": True, "can_run_python": False, "network": None},
        "schema_enforcement": "native",
        "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20, "reasoning_tokens": 5},
        "quota": {"plan_type": "free", "window": "primary", "used_percent_before": 4.0, "used_percent_after": 5.0,
                  "window_minutes": 43200, "resets_at": "2026-11-09T01:53:38-05:00"},
        "timing": {"started": "2026-10-10T12:00:00-04:00", "finished": "2026-10-10T12:03:00-04:00", "seconds": 180.2},
        "warnings": [],
        "error": None,
        "label": None,
    }
    res.update(overrides)
    return res


class ContractFiles(unittest.TestCase):
    def test_contract_files_follow_subset_rules(self):
        self.assertEqual(schema.check_contract(), [])

    def test_answer_schemas_match_the_v0_schemas_codex_accepted(self):
        # docs/spikes.md M0.3: Codex's strict mode was proven on the v0 schemas, so v1 sends the same thing.
        for task_type in ("review", "research"):
            v0 = json.loads((FIXTURES / "v0-schemas" / f"{task_type}.schema.json").read_text(encoding="utf-8"))
            self.assertEqual(schema.strip_annotations(schema.answer_schema(task_type)), v0)

    def test_real_v0_answers_validate(self):
        files = sorted((FIXTURES / "v0-answers").glob("*.json"))
        self.assertTrue(files)
        for f in files:
            task_type = f.stem.rsplit("-", 1)[1]
            answer = json.loads(f.read_text(encoding="utf-8"))
            self.assertEqual(schema.validate(answer, schema.answer_schema(task_type)), [], f.name)


class CheckSchema(unittest.TestCase):
    def test_rejects_keywords_outside_subset(self):
        problems = schema.check_schema({"type": "object", "anyOf": [{"type": "object"}]})
        self.assertTrue(any("anyOf" in p for p in problems))

    def test_rejects_unknown_type_and_missing_type(self):
        self.assertTrue(schema.check_schema({"type": "date"}))
        self.assertTrue(schema.check_schema({"properties": {}}))

    def test_required_must_be_a_property(self):
        self.assertTrue(schema.check_schema({"type": "object", "properties": {}, "required": ["x"]}))

    def test_strict_needs_all_properties_required(self):
        s = {"type": "object", "additionalProperties": False, "required": [],
             "properties": {"x": {"type": "string"}}}
        self.assertEqual(schema.check_schema(s), [])
        self.assertTrue(schema.check_schema(s, strict=True))

    def test_mcp_property_names(self):
        s = {"type": "object", "properties": {"bad name": {"type": "string"}}}
        self.assertTrue(schema.check_schema(s, mcp_names=True))

    def test_enum_values_must_match_type(self):
        self.assertTrue(schema.check_schema({"type": "string", "enum": ["a", 1]}))
        self.assertEqual(schema.check_schema({"type": ["string", "null"], "enum": ["a", None]}), [])


class Validate(unittest.TestCase):
    def test_types(self):
        cases = [("string", "x", True), ("string", 1, False), ("integer", 3, True), ("integer", 3.0, True),
                 ("integer", 3.5, False), ("integer", True, False), ("number", 2, True), ("boolean", 0, False),
                 ("null", None, True), ("array", [], True), ("object", {}, True), (["string", "null"], None, True)]
        for t, value, ok in cases:
            self.assertEqual(schema.validate(value, {"type": t}) == [], ok, (t, value))

    def test_bounds(self):
        self.assertTrue(schema.validate("", {"type": "string", "minLength": 1}))
        self.assertTrue(schema.validate("abc", {"type": "string", "maxLength": 2}))
        self.assertTrue(schema.validate([], {"type": "array", "items": {"type": "string"}, "minItems": 1}))
        self.assertTrue(schema.validate(101, {"type": "number", "maximum": 100}))
        self.assertTrue(schema.validate(-1, {"type": "integer", "minimum": 0}))

    def test_const_and_enum_distinguish_true_from_1(self):
        self.assertTrue(schema.validate(1, {"type": ["integer", "boolean"], "const": True}))
        self.assertEqual(schema.validate(True, {"type": "boolean", "enum": [True]}), [])

    def test_error_paths(self):
        s = {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "string"}}}}
        self.assertEqual(schema.validate({"a": ["x", 2]}, s), ["$.a[1]: expected string, got integer"])


class Requests(unittest.TestCase):
    def test_valid_review_and_research(self):
        self.assertEqual(schema.validate_request(review_request()), [])
        research = review_request(type="research")
        del research["criteria"], research["source_of_truth"]
        self.assertEqual(schema.validate_request(research), [])

    def test_all_optional_fields(self):
        req = review_request(out_of_scope=["style"], notes="n", provider="codex", model="gpt-6-luna",
                             effort="xhigh", access="read", request_key="k1", timeout_s=600)
        self.assertEqual(schema.validate_request(req), [])

    def test_missing_required(self):
        for name in ("workdir", "type", "task", "objective", "inputs"):
            req = review_request()
            del req[name]
            self.assertIn(f"$: missing required field {name!r}", schema.validate_request(req))

    def test_review_needs_criteria_and_source_of_truth(self):
        for name in ("criteria", "source_of_truth"):
            req = review_request()
            del req[name]
            self.assertEqual(schema.validate_request(req), [f"$: review requests need {name!r}"])

    def test_unknown_field_rejected(self):
        self.assertEqual(schema.validate_request(review_request(context="x")), ["$: unknown field 'context'"])

    def test_bad_values(self):
        bad = [dict(type="execute"), dict(access="write"), dict(effort="huge"), dict(provider="gemini"),
               dict(task="x" * 121), dict(inputs=[]), dict(timeout_s=5), dict(timeout_s="900")]
        for override in bad:
            self.assertTrue(schema.validate_request(review_request(**override)), override)

    def test_workdir_must_be_absolute(self):
        self.assertEqual(schema.validate_request(review_request(workdir="vault")),
                         ["$.workdir: must be an absolute path"])
        for ok in ("D:\\Dev\\vault", "D:/Dev/vault", "/home/me/vault", "\\\\server\\share\\vault"):
            self.assertEqual(schema.validate_request(review_request(workdir=ok)), [], ok)

    def test_task_single_line(self):
        self.assertEqual(schema.validate_request(review_request(task="a\nb")), ["$.task: must be a single line"])


class Results(unittest.TestCase):
    def test_valid_done(self):
        self.assertEqual(schema.validate_result(done_result(), "review"), [])

    def test_running_shape(self):
        res = done_result(state="running", answer=None, quota=None)
        res["timing"] = {"started": "2026-10-10T12:00:00-04:00", "finished": None, "seconds": None}
        self.assertEqual(schema.validate_result(res), [])
        res["timing"]["finished"] = "2026-10-10T12:01:00-04:00"
        self.assertEqual(schema.validate_result(res), ["$.timing.finished: must be null while running"])

    def test_every_field_required(self):
        for name in done_result():
            res = done_result()
            del res[name]
            self.assertIn(f"$: missing required field {name!r}", schema.validate_result(res))

    def test_state_and_error_agree(self):
        ok = [("refused", "quota_refused"), ("timed_out", "timeout"), ("failed", "interrupted"),
              ("failed", "provider_error")]
        for state, kind in ok:
            res = done_result(state=state, answer=None, error={"kind": kind, "message": "m"})
            self.assertEqual(schema.validate_result(res), [], (state, kind))
        bad = [("done", "timeout"), ("refused", "timeout"), ("timed_out", "quota_refused"), ("failed", "timeout"),
               ("failed", None), ("refused", None)]
        for state, kind in bad:
            error = {"kind": kind, "message": "m"} if kind else None
            self.assertTrue(schema.validate_result(done_result(state=state, answer=None, error=error)),
                            (state, kind))

    def test_answer_checked_against_type(self):
        res = done_result()
        del res["answer"]["findings"][0]["location"]
        self.assertEqual(schema.validate_result(res), [])  # generic shape allows it
        self.assertEqual(schema.validate_result(res, "review"),
                         ["$.answer.findings[0]: missing required field 'location'"])

    def test_label_shape(self):
        res = done_result(label={"outcome": "useful", "mast": None, "note": None, "labelled": "2026-10-10T12:05:00-04:00"})
        self.assertEqual(schema.validate_result(res), [])
        res["label"]["mast"] = "other"
        self.assertTrue(schema.validate_result(res))

    def test_quota_percent_range(self):
        res = done_result()
        res["quota"] = copy.deepcopy(res["quota"]) | {"used_percent_after": 101}
        self.assertTrue(schema.validate_result(res))


if __name__ == "__main__":
    unittest.main()
