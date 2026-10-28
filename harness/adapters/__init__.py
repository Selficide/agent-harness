"""Provider adapters by name. "fake" is for tests and is not part of the contract's provider enum."""

from .base import ProviderUnavailable  # noqa: F401


def get(name):
    if name == "codex":
        from .codex import CodexAdapter
        return CodexAdapter()
    if name == "claude":
        from .claude import ClaudeAdapter
        return ClaudeAdapter()
    if name == "fake":
        from .fake import FakeAdapter
        return FakeAdapter()
    raise ValueError(f"unknown provider {name!r}")
