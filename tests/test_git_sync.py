"""Tests for the "Plan database sync" feature (app/common.py:
git_plan_db_status, commit_and_push_plan_db, default_plan_db_commit_message).

Exercised against a real local git repo + a bare "remote" on disk (file://
equivalent via a plain path) -- no network, deterministic, fast. This is
the git plumbing behind the Plans-page "Commit & push plan database"
button, added so committing the plan database doesn't require a terminal.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from energyanalyzer.app.common import (
    commit_and_push_plan_db,
    default_plan_db_commit_message,
    git_plan_db_status,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A working repo with a bare 'remote' on disk, a baseline commit under
    plans/ already pushed with upstream tracking set -- mirrors the real
    repo's starting state (plain `git push` with no args must work)."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)

    work = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")

    (work / "plans").mkdir()
    (work / "plans" / "existing_plan.yaml").write_text("id: existing_plan\nretailer: Test Co\n")
    (work / "README.md").write_text("placeholder\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "baseline")
    _git(work, "push", "-q", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")

    return work


def test_status_reports_no_changes_on_clean_repo(repo: Path):
    status = git_plan_db_status(repo_root=repo)
    assert status["changed"] == []
    assert status["branch"] == "main"


def test_status_detects_added_modified_and_untracked_under_plans(repo: Path):
    (repo / "plans" / "existing_plan.yaml").write_text("id: existing_plan\nretailer: New Name\n")
    (repo / "plans" / "new_plan.yaml").write_text("id: new_plan\nretailer: Another Co\n")

    status = git_plan_db_status(repo_root=repo)
    paths = {c["path"]: c["status"] for c in status["changed"]}
    assert paths["plans/existing_plan.yaml"] == "M"
    assert paths["plans/new_plan.yaml"] == "??"


def test_status_ignores_changes_outside_plans(repo: Path):
    (repo / "README.md").write_text("changed\n")
    status = git_plan_db_status(repo_root=repo)
    assert status["changed"] == []


def test_default_commit_message_summarizes_counts():
    changed = [
        {"status": "M", "path": "plans/a.yaml"},
        {"status": "??", "path": "plans/b.yaml"},
        {"status": "D", "path": "plans/c.yaml"},
    ]
    msg = default_plan_db_commit_message(changed)
    assert "1 added" in msg
    assert "1 modified" in msg
    assert "1 deleted" in msg


def test_default_commit_message_no_changes():
    assert "no changes" in default_plan_db_commit_message([])


def test_commit_and_push_happy_path(repo: Path):
    (repo / "plans" / "new_plan.yaml").write_text("id: new_plan\nretailer: Another Co\n")

    result = commit_and_push_plan_db("Add new_plan", repo_root=repo)

    assert result == {"committed": True, "pushed": True, "note": None, "error": None}
    assert git_plan_db_status(repo_root=repo)["changed"] == []

    log = _git(repo, "log", "--oneline", "-1", "origin/main").stdout
    assert "Add new_plan" in log


def test_commit_and_push_nothing_to_commit(repo: Path):
    result = commit_and_push_plan_db("nothing here", repo_root=repo)
    assert result == {"committed": False, "pushed": False, "note": "Nothing to commit.", "error": None}


def test_commit_and_push_never_touches_files_outside_plans(repo: Path):
    (repo / "README.md").write_text("changed outside plans\n")
    (repo / "plans" / "new_plan.yaml").write_text("id: new_plan\nretailer: Another Co\n")

    commit_and_push_plan_db("Add new_plan only", repo_root=repo)

    # README.md change should still be sitting there, uncommitted.
    status = _git(repo, "status", "--porcelain", "--", "README.md").stdout
    assert "README.md" in status


def test_commit_and_push_reports_rejected_push_without_raising(repo: Path):
    # Simulate another session pushing to the remote first, so this
    # checkout's push is a rejected non-fast-forward -- must be reported,
    # not raised, and the local commit must still exist.
    other = repo.parent / "other_clone"
    subprocess.run(["git", "clone", "-q", str(repo.parent / "remote.git"), str(other)], check=True)
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    (other / "plans" / "from_other_session.yaml").write_text("id: from_other_session\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "from another session")
    _git(other, "push", "-q", "origin", "HEAD:main")

    (repo / "plans" / "new_plan.yaml").write_text("id: new_plan\nretailer: Another Co\n")
    result = commit_and_push_plan_db("Add new_plan", repo_root=repo)

    assert result["committed"] is True
    assert result["pushed"] is False
    assert result["error"] is not None
    assert "push failed" in result["error"].lower()
    # The commit is still there locally even though the push was rejected.
    log = _git(repo, "log", "--oneline", "-1").stdout
    assert "Add new_plan" in log
