"""Command line for the harness (contract v1). Results print as JSON; report and doctor print text unless --json.

  python -m harness delegate <request.json | -> [--detach]
  python -m harness result <run_id> [--full]
  python -m harness reply <run_id> <file> [--detach]
  python -m harness label <run_id> --outcome useful|partial|useless [--mast CATEGORY] [--note TEXT]
  python -m harness report [--workdir DIR] [--limit N] [--json]
  python -m harness doctor [--json]
  python -m harness migrate-v0 <v0_runs_dir>

Exit codes: 0 the operation ran (check the result's state), 1 doctor found a problem, 2 the request was refused
before a run existed (the error prints as JSON).
"""

import argparse
import json
import sys
from pathlib import Path

from . import core, v0


def _print(data):
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _progress(message, elapsed):
    print(f"[{elapsed}s] {message}", file=sys.stderr, flush=True)


def _read(path):
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def cmd_delegate(args):
    try:
        request = json.loads(_read(args.request))
    except ValueError as e:
        raise core.HarnessError("invalid_request", f"request is not valid JSON: {e}")
    _print(core.delegate(request, wait=not args.detach, progress=_progress, provider=args.provider))


def cmd_result(args):
    _print(core.result(args.run_id, full=args.full))


def cmd_reply(args):
    _print(core.reply(args.run_id, _read(args.file), wait=not args.detach, progress=_progress))


def cmd_label(args):
    _print(core.label(args.run_id, args.outcome, args.mast, args.note))


def cmd_report(args):
    rep = core.report(workdir=args.workdir, limit=args.limit)
    if args.json:
        return _print(rep)
    for r in rep["runs"]:
        print(f"{r['run_id'][:56]:<56} {r['state']:<9} {r['type']:<8} {r['effort'] or '-':<6} {r['turns']} turn(s) "
              f"{r['tokens']:>8} tok  quota {r['quota'] or '-':<10} {r['outcome']}")
    t = rep["totals"]
    print(f"\n{t['runs']} runs | {t['input_tokens']} input + {t['output_tokens']} output tokens | "
          + ", ".join(f"{k}={v}" for k, v in sorted(t["outcomes"].items())))
    if rep["in_flight"]:
        print("in flight: " + ", ".join(rep["in_flight"]))
    for q in rep["pending_questions"]:
        print(f"waiting for a reply: {q['run_id']}: {' / '.join(q['questions'] or [])}")
    for provider, snap in rep["quota_now"].items():
        for w in (snap or {}).get("windows", []):
            print(f"quota ({provider}, {snap.get('plan_type')}, {w['name']}): {w['used_percent']:g}% used, "
                  f"resets {w['resets_at']}")
    for e in rep["quota_updates"][:5]:
        print(f"{e['at']}  {e['message']}")


def cmd_doctor(args):
    rep = core.doctor()
    if args.json:
        _print(rep)
    else:
        for c in rep["checks"]:
            print(f"[{c['status']}] {c['check']}: {c['detail']}")
    return 0 if rep["ok"] else 1


def cmd_migrate(args):
    copied, skipped = v0.migrate(args.src)
    print(f"copied {len(copied)} v0 run(s), skipped {len(skipped)} already in the store; originals left in place")
    for name in copied:
        print(f"  + {name}")


def build_parser():
    p = argparse.ArgumentParser(prog="python -m harness", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("delegate", help="start a run from a request file and wait for its result")
    d.add_argument("request", help="request JSON file, or - for stdin")
    d.add_argument("--detach", action="store_true", help="return at once with state running")
    d.add_argument("--provider", help=argparse.SUPPRESS)  # tests: "fake"
    d.set_defaults(func=cmd_delegate)

    r = sub.add_parser("result", help="a run's current result")
    r.add_argument("run_id")
    r.add_argument("--full", action="store_true", help="don't trim findings to fit inline")
    r.set_defaults(func=cmd_result)

    rp = sub.add_parser("reply", help="answer a blocked run's question and wait")
    rp.add_argument("run_id")
    rp.add_argument("file", help="reply text file, or - for stdin")
    rp.add_argument("--detach", action="store_true")
    rp.set_defaults(func=cmd_reply)

    lb = sub.add_parser("label", help="record how useful a run was")
    lb.add_argument("run_id")
    lb.add_argument("--outcome", required=True, choices=["useful", "partial", "useless"])
    lb.add_argument("--mast", choices=["specification", "coordination", "verification"],
                    help="failure category, if the run failed")
    lb.add_argument("--note")
    lb.set_defaults(func=cmd_label)

    rep = sub.add_parser("report", help="recent and in-flight runs, pending questions, totals, quota")
    rep.add_argument("--workdir", help="only runs in this folder")
    rep.add_argument("--limit", type=int, default=50)
    rep.add_argument("--json", action="store_true")
    rep.set_defaults(func=cmd_report)

    doc = sub.add_parser("doctor", help="check the provider, sandbox, store, and quota")
    doc.add_argument("--json", action="store_true")
    doc.set_defaults(func=cmd_doctor)

    m = sub.add_parser("migrate-v0", help="copy v0 run folders into the store (originals are kept)")
    m.add_argument("src")
    m.set_defaults(func=cmd_migrate)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except core.HarnessError as e:
        _print(e.as_dict())
        return 2
