"""Representative GitHub data the Messages page previews every card from (and the tests render).

One fictional organization, `formicaria`, and its `anthill` repository; the shapes are GitHub's own (webhook
payloads, Actions API objects) trimmed to the fields the renderers read. Everything is fixed — no clocks, no
random values — so a preview looks the same every time.
"""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any

ORG = "formicaria"
REPO_URL = f"https://github.com/{ORG}/anthill"
REPO = {"name": "anthill", "full_name": f"{ORG}/anthill", "html_url": REPO_URL, "default_branch": "main",
        "description": "Pheromone-routed task queue for the colony", "private": False,
        "stargazers_count": 42, "forks_count": 6, "open_issues_count": 5, "owner": {"login": ORG}}

ALICE = {"login": "alice", "type": "User", "html_url": "https://github.com/alice",
         "avatar_url": "https://avatars.githubusercontent.com/u/1?v=4"}
BOB = {"login": "bob", "type": "User", "html_url": "https://github.com/bob",
       "avatar_url": "https://avatars.githubusercontent.com/u/2?v=4"}
CAROL = {"login": "carol", "type": "User", "html_url": "https://github.com/carol",
         "avatar_url": "https://avatars.githubusercontent.com/u/3?v=4"}

HEAD_SHA = "9fceb02d3a1b4c5d6e7f8091a2b3c4d5e6f70819"
BASE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

PULL_REQUEST = {
    "number": 42, "title": "Add pheromone router", "html_url": f"{REPO_URL}/pull/42", "state": "open",
    "draft": False, "merged": False, "user": ALICE,
    "body": "Routes tasks along the strongest trail instead of round-robin, so hot queues drain first.\n\nCloses #17.",
    "base": {"ref": "main", "sha": BASE_SHA},
    "head": {"ref": "feat/router", "label": "alice:feat/router", "sha": HEAD_SHA},
    "additions": 120, "deletions": 14, "changed_files": 6, "labels": [{"name": "enhancement"}],
}

ISSUE = {
    "number": 17, "title": "Workers starve when the queue is empty", "html_url": f"{REPO_URL}/issues/17",
    "state": "open", "user": BOB,
    "body": "After the queue drains, workers spin at 100% CPU polling for tasks instead of backing off.\n\n"
            "Seen on every node since 2.0.3.",
    "labels": [{"name": "bug"}, {"name": "priority:high"}], "assignees": [ALICE],
}

RELEASE = {
    "name": "v2.1.0 — Pheromone router", "tag_name": "v2.1.0", "html_url": f"{REPO_URL}/releases/tag/v2.1.0",
    "prerelease": False, "draft": False, "author": ALICE,
    "body": "## Highlights\n- Pheromone router: tasks follow the strongest trail (#42)\n"
            "- Idle workers back off instead of spinning (#17)\n- Python 3.13 wheels",
    "assets": [
        {"name": "anthill-2.1.0-linux-amd64.tar.gz", "size": 8_421_376,
         "browser_download_url": f"{REPO_URL}/releases/download/v2.1.0/anthill-2.1.0-linux-amd64.tar.gz"},
        {"name": "anthill-2.1.0-linux-arm64.tar.gz", "size": 8_118_272,
         "browser_download_url": f"{REPO_URL}/releases/download/v2.1.0/anthill-2.1.0-linux-arm64.tar.gz"},
    ],
}

WORKFLOW_RUN = {
    "id": 9001, "name": "CI", "run_number": 91, "event": "push", "status": "completed", "conclusion": "success",
    "head_branch": "main", "head_sha": HEAD_SHA, "display_title": "Add pheromone router (#42)",
    "html_url": f"{REPO_URL}/actions/runs/9001", "created_at": "2026-09-02T13:59:58Z",
    "run_started_at": "2026-09-02T14:00:00Z", "updated_at": "2026-09-02T14:04:12Z",
    "actor": ALICE, "triggering_actor": ALICE,
}

DISCUSSION = {
    "number": 23, "title": "How should idle workers back off?", "html_url": f"{REPO_URL}/discussions/23",
    "category": {"name": "Ideas"}, "user": CAROL,
    "body": "Exponential back-off is the obvious answer, but a colony-wide pheromone decay might be simpler.",
}

COMMITS = [
    {"id": BASE_SHA, "message": "router: follow the strongest trail\n\nHot queues drain first.",
     "author": {"name": "alice"}, "url": f"{REPO_URL}/commit/{BASE_SHA}"},
    {"id": "1f7ec0a5b3d94c2e8a6f0b1c3d5e7f9a2b4c6d8e", "message": "workers: back off when the queue is empty",
     "author": {"name": "bob"}, "url": f"{REPO_URL}/commit/1f7ec0a5b3d94c2e8a6f0b1c3d5e7f9a2b4c6d8e"},
    {"id": HEAD_SHA, "message": "docs: describe the router", "author": {"name": "alice"},
     "url": f"{REPO_URL}/commit/{HEAD_SHA}"},
]


def _base(sender: dict[str, Any] = ALICE, **parts: Any) -> dict[str, Any]:
    p: dict[str, Any] = {"repository": REPO, "organization": {"login": ORG}, "sender": sender}
    p.update(parts)
    return p


# (webhook event name, payload) for every feed kind; the key is the kind's name (`github.<key>`)
EVENTS: dict[str, tuple[str, dict[str, Any]]] = {
    "push": ("push", _base(ref="refs/heads/main", before="c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4", after=HEAD_SHA,
                           commits=COMMITS, forced=False, deleted=False, created=False,
                           compare=f"{REPO_URL}/compare/c3d2e1f0a9b8...9fceb02d3a1b", pusher={"name": "alice"})),
    "create": ("create", _base(ref_type="branch", ref="feat/router", master_branch="main")),
    "delete": ("delete", _base(ref_type="branch", ref="feat/old-scheduler")),
    "pull_request": ("pull_request", _base(action="opened", number=42, pull_request=PULL_REQUEST)),
    "pull_request_review": ("pull_request_review", _base(
        BOB, action="submitted", pull_request=PULL_REQUEST,
        review={"state": "approved", "body": "LGTM — nice work on the router tests.", "user": BOB,
                "html_url": f"{REPO_URL}/pull/42#pullrequestreview-77"})),
    "issues": ("issues", _base(BOB, action="opened", issue=ISSUE)),
    "issue_comment": ("issue_comment", _base(
        CAROL, action="created", issue=ISSUE,
        comment={"body": "Reproduced on pve2 as well — the poll loop never sleeps.", "user": CAROL,
                 "html_url": f"{REPO_URL}/issues/17#issuecomment-1234"})),
    "release": ("release", _base(action="published", release=RELEASE)),
    "workflow_run": ("workflow_run", _base(action="completed", workflow_run=WORKFLOW_RUN, workflow={"name": "CI"})),
    "star": ("star", _base(BOB, action="created")),
    "fork": ("fork", _base(BOB, forkee={"full_name": "bob/anthill", "html_url": "https://github.com/bob/anthill",
                                          "owner": {"login": "bob"}})),
    "repository": ("repository", _base(action="created", repository={
        "name": "micromound", "full_name": f"{ORG}/micromound", "html_url": f"https://github.com/{ORG}/micromound",
        "default_branch": "main", "description": "Tiny habitat controller", "private": True, "owner": {"login": ORG}})),
    "member": ("member", _base(action="added", member=BOB)),
    "organization": ("organization", {"action": "member_added", "organization": {"login": ORG}, "sender": ALICE,
                                      "membership": {"role": "member", "user": CAROL}}),
    "deployment_status": ("deployment_status", _base(
        action="created", deployment={"ref": "main", "sha": HEAD_SHA, "environment": "production", "task": "deploy"},
        deployment_status={"state": "success", "environment": "production", "description": "Rolled out to 3 nodes",
                           "environment_url": "https://anthill.example", "target_url": f"{REPO_URL}/deployments"})),
    "discussion": ("discussion", _base(CAROL, action="created", discussion=DISCUSSION)),
    "discussion_comment": ("discussion_comment", _base(
        ALICE, action="created", discussion=DISCUSSION,
        comment={"body": "Decay is simpler and it composes with the router — let's try it.", "user": ALICE,
                 "html_url": f"{REPO_URL}/discussions/23#discussioncomment-99"})),
    "check_run": ("check_run", _base(action="completed", check_run={
        "name": "lint", "status": "completed", "conclusion": "failure", "head_sha": HEAD_SHA,
        "html_url": f"{REPO_URL}/runs/5150", "output": {"title": "3 errors", "summary": "ruff found 3 problems"}})),
    # an event without a card of its own: verbose mode's generic one-liner
    "other": ("milestone", _base(action="created", milestone={"title": "v2.2", "number": 4,
                                                                 "html_url": f"{REPO_URL}/milestone/4"})),
}


def payload(kind: str) -> tuple[str, dict[str, Any]]:
    """A fresh (event, payload) for a feed kind's name, safe to modify."""
    return copy.deepcopy(EVENTS[kind])


# ----- the live CI train card: a run in progress ---------------------------------------------
TRAIN_RUN = {
    **WORKFLOW_RUN, "id": 9002, "run_number": 92, "status": "in_progress", "conclusion": None,
    "display_title": "workers: back off when the queue is empty", "html_url": f"{REPO_URL}/actions/runs/9002",
    "run_started_at": "2026-09-02T15:00:00Z", "updated_at": "2026-09-02T15:01:30Z", "triggering_actor": BOB, "actor": BOB,
}
TRAIN_NOW = dt.datetime(2026, 9, 2, 15, 1, 30, tzinfo=dt.timezone.utc)   # the clock the elapsed time is read from
TRAIN_JOBS = [
    {"name": "lint", "status": "completed", "conclusion": "success",
     "started_at": "2026-09-02T15:00:05Z", "completed_at": "2026-09-02T15:00:17Z"},
    {"name": "test (core)", "status": "in_progress", "conclusion": None, "started_at": "2026-09-02T15:00:20Z",
     "steps": [{"name": "checkout", "status": "completed"}, {"name": "install", "status": "completed"},
               {"name": "pytest", "status": "in_progress"}, {"name": "upload coverage", "status": "queued"}]},
    {"name": "test (arr)", "status": "queued", "conclusion": None},
    {"name": "docker", "status": "queued", "conclusion": None},
]


def train() -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """(repo, run, jobs) for the CI train preview, safe to modify."""
    return "anthill", copy.deepcopy(TRAIN_RUN), copy.deepcopy(TRAIN_JOBS)


# ----- the org overview board -------------------------------------------------------------------
BOARD_REPOS = [{"name": n} for n in ("anthill", "micromound", "periscope", "sovrgn", "trailmap", "nestwatch")]
BOARD_CI = {
    "anthill": {"ok": True, "name": "CI", "url": f"{REPO_URL}/actions/runs/9001"},
    "micromound": {"ok": True, "name": "build", "url": f"https://github.com/{ORG}/micromound/actions/runs/311"},
    "periscope": {"ok": False, "name": "CI", "url": f"https://github.com/{ORG}/periscope/actions/runs/2048"},
}
BOARD_RECENT = [
    f"<t:1788357852:R> [[anthill] ✅ CI success on main]({REPO_URL}/actions/runs/9001)",
    f"<t:1788356460:R> [[anthill] PR #42 opened: Add pheromone router]({REPO_URL}/pull/42)",
    f"<t:1788351300:R> [[anthill:main] 3 new commits]({REPO_URL}/compare/c3d2e1f0a9b8...9fceb02d3a1b)",
    f"<t:1788341400:R> [[anthill] Issue #17 opened: Workers starve when the queue is empty]({REPO_URL}/issues/17)",
    f"<t:1788286800:R> [[periscope] ❌ CI failure on main](https://github.com/{ORG}/periscope/actions/runs/2048)",
]


def board() -> dict[str, Any]:
    """The pieces `render.board_ctx` is built from, as the board cog gathers them (API up, polling on)."""
    return {"org": ORG, "repos": copy.deepcopy(BOARD_REPOS), "prs": {"total_count": 3}, "issues": {"total_count": 11},
            "ci": copy.deepcopy(BOARD_CI), "recent": list(BOARD_RECENT), "poll": True, "api_ok": True}
