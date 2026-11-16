"""Regression tests for the release gate's fail-closed behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.release_gate import REQUIRED_WORKFLOWS, release_is_ready

SHA = "f" * 40


def _run(
    name: str,
    *,
    sha: str = SHA,
    event: str = "push",
    branch: str = "main",
    status: str = "completed",
    conclusion: str | None = "success",
    run_id: int = 1,
) -> dict:
    return {
        "name": name,
        "head_sha": sha,
        "head_branch": branch,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "created_at": f"2026-11-16T{run_id:02d}:00:00Z",
        "run_attempt": 1,
        "id": run_id,
    }


def _green_runs() -> list[dict]:
    return [_run(REQUIRED_WORKFLOWS[0], run_id=1), _run(REQUIRED_WORKFLOWS[1], run_id=2)]


def test_both_successful_workflows_approve_release() -> None:
    ready, _ = release_is_ready({"total_count": 2, "workflow_runs": _green_runs()}, SHA)
    assert ready


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [_run(REQUIRED_WORKFLOWS[0])],  # HACS/Hassfest missing
        [_run(REQUIRED_WORKFLOWS[0], conclusion="failure"), _run(REQUIRED_WORKFLOWS[1])],
        [_run(REQUIRED_WORKFLOWS[0], status="in_progress", conclusion=None), _run(REQUIRED_WORKFLOWS[1])],
        [_run(REQUIRED_WORKFLOWS[0], sha="a" * 40), _run(REQUIRED_WORKFLOWS[1])],
        [_run(REQUIRED_WORKFLOWS[0], branch="feature"), _run(REQUIRED_WORKFLOWS[1])],
        [_run(REQUIRED_WORKFLOWS[0], event="pull_request"), _run(REQUIRED_WORKFLOWS[1])],
        [_run(REQUIRED_WORKFLOWS[0], event="schedule"), _run(REQUIRED_WORKFLOWS[1])],
    ],
)
def test_missing_failed_pending_or_untrusted_runs_never_release(runs: list[dict]) -> None:
    assert not release_is_ready({"total_count": len(runs), "workflow_runs": runs}, SHA)[0]


def test_latest_failed_attempt_overrides_earlier_success() -> None:
    runs = _green_runs() + [_run(REQUIRED_WORKFLOWS[0], run_id=3, conclusion="failure")]
    assert not release_is_ready({"total_count": len(runs), "workflow_runs": runs}, SHA)[0]


def test_truncated_api_results_never_release() -> None:
    assert not release_is_ready({"total_count": 101, "workflow_runs": _green_runs()}, SHA)[0]


def test_dependabot_dispatched_checks_can_release() -> None:
    runs = [_run(name, event="workflow_dispatch", run_id=i) for i, name in enumerate(REQUIRED_WORKFLOWS, 1)]
    assert release_is_ready({"total_count": len(runs), "workflow_runs": runs}, SHA)[0]


def test_release_does_not_run_on_main_push_directly() -> None:
    path = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert "push" not in workflow["on"]
    assert workflow["on"]["workflow_run"]["types"] == ["completed"]
    assert workflow["on"]["workflow_run"]["branches"] == ["main"]
    assert workflow["on"]["workflow_run"]["workflows"] == list(REQUIRED_WORKFLOWS)
