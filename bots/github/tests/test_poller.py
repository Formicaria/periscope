"""Workflow-run polling: baseline silently, then announce started → completed for every run (CI, release builds…)."""

import pytest
from periscope.state import JsonState

from periscope_github.cogs import poller as poller_mod
from periscope_github.config import GithubSettings


class FakeClient:
    def __init__(self):
        self.runs = {"periscope": []}

    async def list_repos(self):
        return [{"name": n} for n in self.runs]

    async def workflow_runs(self, repo, per_page=10, branch=None):
        return list(reversed(self.runs[repo]))  # GitHub returns newest first


class FakeDispatcher:
    def __init__(self):
        self.calls = []

    async def dispatch(self, event, payload, **kw):
        self.calls.append((event, payload["action"], payload["workflow_run"]["id"], payload["workflow_run"]["name"]))
        return True


class FakeBot:
    def __init__(self, tmp_path, cfg, client):
        self.state = JsonState(tmp_path / "state.json")
        self.gh_settings = cfg
        self.gh_client = client
        self.settings = type("S", (), {"alert_channel_id": None})()
        self.alerts = None

    async def wait_until_ready(self):
        pass


def run(rid, status, name="ci", conclusion=None):
    return {"id": rid, "name": name, "status": status, "conclusion": conclusion, "head_branch": "main",
            "head_sha": "abc1234", "html_url": f"r/{rid}", "run_number": rid, "updated_at": "2026-09-02T00:00:00Z",
            "triggering_actor": {"login": "github-actions[bot]"}}


@pytest.mark.asyncio
async def test_poll_ci_announces_start_and_finish(tmp_path, monkeypatch):
    client = FakeClient()
    cfg = GithubSettings(poll_enabled=False)
    bot = FakeBot(tmp_path, cfg, client)
    disp = FakeDispatcher()
    monkeypatch.setattr(poller_mod, "get_dispatcher", lambda b: disp)
    p = poller_mod.GithubPoller(bot)
    tick = p.poll_ci.coro

    client.runs["periscope"] = [run(1, "completed", conclusion="success")]
    await tick(p)
    assert disp.calls == []                                   # baseline: history is never replayed

    client.runs["periscope"].append(run(2, "in_progress", name="release"))
    await tick(p)
    assert disp.calls == [("workflow_run", "in_progress", 2, "release")]

    await tick(p)
    assert len(disp.calls) == 1                               # still running: announced once

    client.runs["periscope"][-1] = run(2, "completed", name="release", conclusion="success")
    client.runs["periscope"].append(run(3, "completed", conclusion="failure"))  # finished between polls
    await tick(p)
    assert disp.calls[1:] == [("workflow_run", "completed", 2, "release"), ("workflow_run", "completed", 3, "ci")]

    await tick(p)
    assert len(disp.calls) == 3                               # nothing new → nothing posted
