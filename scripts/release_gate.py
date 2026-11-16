"""Only allow releases after both required GitHub Actions workflows pass for main."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

REQUIRED_WORKFLOWS = ("Tests", "Validate (Marketplace / HACS + Hassfest)")
ALLOWED_EVENTS = {"push", "workflow_dispatch"}


def release_is_ready(payload: dict[str, Any], sha: str) -> tuple[bool, str]:
    """Check the latest CI runs for *exactly* the target main commit.

    A missing, failing, cancelled or still-running workflow must never publish a
    release. A successful earlier run must not mask a more recent failed rerun.
    """
    runs = payload.get("workflow_runs", [])
    if not isinstance(runs, list):
        return False, "GitHub Actions did not return workflow runs"
    if payload.get("total_count", len(runs)) > len(runs):
        return False, "GitHub Actions response was truncated; refusing to release"

    for name in REQUIRED_WORKFLOWS:
        candidates = [
            run
            for run in runs
            if run.get("name") == name
            and run.get("head_sha") == sha
            and run.get("head_branch") == "main"
            and run.get("event") in ALLOWED_EVENTS
        ]
        if not candidates:
            return False, f"{name}: no CI run for commit {sha}"

        latest = max(
            candidates,
            key=lambda run: (
                run.get("created_at") or "",
                int(run.get("run_attempt") or 0),
                int(run.get("id") or 0),
            ),
        )
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            return False, (
                f"{name}: latest run {latest.get('id')} is "
                f"{latest.get('status')} / {latest.get('conclusion')}"
            )

    return True, f"Both required workflows passed for {sha}"


def main() -> None:
    """Query GitHub using the job token; report the gate decision to Actions."""
    repository = os.environ["GITHUB_REPOSITORY"]
    target_sha = os.environ["VALIDATED_SHA"]
    result = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            "GET",
            f"/repos/{repository}/actions/runs?head_sha={target_sha}&per_page=100",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    ready, reason = release_is_ready(json.loads(result.stdout), target_sha)
    print(reason)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"ready={str(ready).lower()}\n")


if __name__ == "__main__":
    main()
