"""The adapter interface. An adapter knows one provider's CLI; the worker runs the commands it builds."""


class ProviderUnavailable(Exception):
    """The provider's CLI is missing or unusable."""


class Adapter:
    name = None
    schema_enforcement = "native"   # or "validated": prompt plus check plus one retry
    prompt_version = None
    types = ("review", "research")   # request types this provider takes
    instruction_files = ()           # files the provider loads as instructions from workdir upwards
    instructions_up_to_git_root = True

    def defaults(self, cfg):
        """{"model": ..., "effort": {type: effort}} for requests that don't say."""
        raise NotImplementedError

    def capabilities(self):
        """{can_read_files, can_run_commands, can_run_python, network}: booleans, or None when undetected."""
        raise NotImplementedError

    def render(self, request):
        """The prompt text for a new run."""
        raise NotImplementedError

    def command(self, run, turn):
        """(argv, cwd) for turn N (1 = new run, >1 = a reply). run is the meta dict plus run_dir and request.
        The prompt or reply text goes to stdin; output files use run_files(turn)."""
        raise NotImplementedError

    def parse(self, run_dir, turn):
        """Read turn N's output files: {thread_id, usage, answer_text, errors, commands} (commands: the shell
        commands the provider ran, as text, for the coverage check)."""
        raise NotImplementedError

    def env(self, base):
        """The environment for the provider's process, from the worker's own."""
        return base

    def quota_snapshot(self, thread_id=None):
        """A quota snapshot (see quota.py): the run's own latest one when thread_id is given, else the newest
        known. None when the provider doesn't report one."""
        return None

    def version(self):
        return None

    def doctor(self):
        """[(check, ok, detail)]"""
        return []


def run_files(turn):
    """File names for turn N: no suffix for turn 1, -N after."""
    suffix = "" if turn == 1 else f"-{turn}"
    return {
        "events": f"events{suffix}.jsonl",
        "stderr": f"stderr{suffix}.log",
        "answer": f"result{suffix}.md",
        "input": "prompt.md" if turn == 1 else f"reply{suffix}.md",
    }
