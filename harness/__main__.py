import sys

if len(sys.argv) > 1 and sys.argv[1] == "worker":
    import argparse

    from . import worker

    p = argparse.ArgumentParser(prog="python -m harness worker")
    p.add_argument("command")
    p.add_argument("run_id")
    p.add_argument("--turn", type=int, required=True)
    args = p.parse_args()
    sys.exit(worker.main(args.run_id, args.turn))

from .cli import main  # noqa: E402

sys.exit(main())
