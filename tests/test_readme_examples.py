"""The README's failed-task example, run, and the public text it ships with."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from wauldo import TaskErrorInfo

ROOT = Path(__file__).resolve().parent.parent


def _failed_task_example() -> str:
    """The README block that handles a failed task, from its ``if`` to the fence."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    start = readme.index('if task.status == "failed":')
    return readme[start : readme.index("```", start)]


class _Agents:
    def __init__(self) -> None:
        self.resumed: list[str] = []

    def resume_task(self, task_id: str) -> SimpleNamespace:
        self.resumed.append(task_id)
        return SimpleNamespace(task_id=f"{task_id}-retry")


def _run_example(error: str) -> list[str]:
    agents = _Agents()
    task = SimpleNamespace(
        status="failed", task_id="t1", error_info=TaskErrorInfo.parse(error)
    )
    exec(_failed_task_example(), {"task": task, "agents": agents, "print": lambda *_: None})
    return agents.resumed


@pytest.mark.parametrize(
    "error",
    [
        "run_failed: The agent run failed. Retry later.",
        "quota_exceeded: a code this SDK version does not know",
        "internal error: a code-less error",
    ],
)
def test_readme_failed_task_example_retries_everything_but_agent_errors(error: str) -> None:
    assert _run_example(error) == ["t1"]


@pytest.mark.parametrize(
    "error",
    ["invalid_preset: Unknown preset: x", "invalid_agent_config: bad workflow"],
)
def test_readme_failed_task_example_does_not_retry_agent_errors(error: str) -> None:
    assert _run_example(error) == []


_INTERNAL = re.compile(
    r"wauldo-(api|core|llm|tools|quality|orchestrator|cognition|classifier)\b|routes::"
)


def test_shipped_sources_name_no_server_internals() -> None:
    files = [ROOT / "README.md", *sorted((ROOT / "src").rglob("*.py"))]
    hits = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in files
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _INTERNAL.search(line)
    ]
    assert hits == []
