"""v2: the github service built on a shared presence, its /github route on the shared server, and its check."""

import asyncio
import hashlib
import hmac
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from discord import app_commands
from periscope import Store
from periscope.http import HttpClient, HttpError
from periscope.runtime import Runtime

from periscope_github.service import SERVICES

EXPECTED = {"repos", "repo", "prs", "issues", "runs", "commits", "activity", "watch"}
ENV = {"GITHUB_ORG": "formicaria", "GITHUB_TOKEN": "ghp_x", "GITHUB_POLL_ENABLED": "false",
       "GITHUB_REPO_CHANNEL_MAP": "periscope=11", "GITHUB_CI_CHANNEL_ID": "12"}


def test_spec():
    (spec,) = SERVICES
    assert spec.name == "github" and spec.slash == "/gh" and spec.group == "dev"
    assert spec.needs_webhook and spec.webhook_paths == ["/github"]
    keys = {s.key: s for s in spec.settings}
    assert set(keys) == {"GITHUB_ORG", "GITHUB_TOKEN", "GITHUB_REPO_CHANNEL_MAP", "GITHUB_FEED_CHANNEL_ID", "GITHUB_CI_CHANNEL_ID",
                         "GITHUB_MIRROR_TO_FEED", "GITHUB_EVENTS", "GITHUB_IGNORE_BOTS", "GITHUB_VERBOSE",
                         "GITHUB_CI_FAILURE_ROLE_ID", "GITHUB_POLL_ENABLED", "GITHUB_POLL_INTERVAL_S"}
    assert keys["GITHUB_TOKEN"].type == "secret" and keys["GITHUB_REPO_CHANNEL_MAP"].type == "list"
    assert keys["GITHUB_CI_CHANNEL_ID"].type == "channel" and keys["GITHUB_CI_FAILURE_ROLE_ID"].type == "role"
    assert spec.required_missing({}) == []                              # the org defaults, the token is optional


@pytest.mark.asyncio
async def test_build_on_presence(tmp_path):
    s = Store(tmp_path / "config" / "periscope.yaml")
    s.presences["default"]["token"] = "T"
    s.lab.update({"alert_channel_id": "2", "status_channel_id": "1"})
    s.webhook["secret"] = "hook"
    s.services["github"] = {"enabled": True, "presence": "default", "env": ENV}
    rt = Runtime(s, tmp_path)
    rt.assemble()
    assert not rt.skipped
    pres, sb = rt.presences["default"], rt.services["github"]

    async def never_ready():
        await asyncio.Event().wait()

    pres.wait_until_ready = never_ready
    await sb.spec.build(sb)
    assert sb.gh_settings.org == "formicaria" and sb.gh_settings.repo_channel_map == {"periscope": 11}
    assert sb.gh_client.org == "formicaria" and not sb.gh_settings.poll_enabled
    assert sb.ci_trains is not None and sb.gh_dispatcher.cfg is sb.gh_settings          # what build_bot + the cogs wire
    group = pres.tree.get_command("gh")
    assert isinstance(group, app_commands.Group) and {c.name for c in group.commands} == EXPECTED
    names = {c.qualified_name for c in pres.cogs.values()}
    assert names == {"github:GithubEvents", "github:GithubCommands", "github:GithubPoller"}
    assert sb.gh_settings.channels_for("periscope", "workflow_run", sb.settings.alert_channel_id) == [11]
    assert sb.gh_settings.channels_for("other", "workflow_run", sb.settings.alert_channel_id) == [12]

    # the webhook lands on the shared server with the store's secret (GitHub-style HMAC)
    body = json.dumps({"zen": "Keep it logically awesome.", "hook": {"config": {"url": "https://lab/github"}}}).encode()
    sig = "sha256=" + hmac.new(b"hook", body, hashlib.sha256).hexdigest()
    async with TestClient(TestServer(rt.webhook.app)) as client:
        r = await client.post("/github", data=body, headers={"X-GitHub-Event": "ping"})
        assert r.status == 401
        r = await client.post("/github", data=body, headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sig})
        assert r.status == 200 and await r.json() == {"ok": True, "pong": True}
    await sb.unload()


@pytest.mark.asyncio
async def test_check(monkeypatch):
    calls = []

    async def get_json(self, path, **kw):
        auth = self._headers.get("Authorization")
        calls.append((self.base_url, path, auth))
        if self.base_url.startswith("https://down"):
            raise OSError("timed out")
        if path == "/user":
            if auth != "Bearer good":
                raise HttpError(401, self.base_url + path, "Bad credentials")
            return {"login": "xchronusx"}
        if path == "/orgs/formicaria":
            return {"login": "formicaria", "public_repos": 4, "total_private_repos": 2 if auth else None}
        raise HttpError(404, self.base_url + path, "Not Found")

    monkeypatch.setattr(HttpClient, "get_json", get_json)
    (check,) = [s.check for s in SERVICES]
    assert await check({"GITHUB_ORG": "formicaria", "GITHUB_TOKEN": "good"}) == (True, "GitHub token works — xchronusx; formicaria: 6 repos visible")
    assert calls[0][:2] == ("https://api.github.com", "/user")
    ok, msg = await check({"GITHUB_ORG": "formicaria"})
    assert ok and "webhook-only" in msg and "4 public repos" in msg and calls[-1][2] is None
    ok, msg = await check({"GITHUB_TOKEN": "bad"})
    assert not ok and "401" in msg and "GITHUB_TOKEN" in msg
    ok, msg = await check({"GITHUB_ORG": "nobody", "GITHUB_TOKEN": "good"})
    assert not ok and "nobody" in msg and "not found" in msg
    ok, msg = await check({"GITHUB_ORG": "nobody"})
    assert not ok and "GITHUB_ORG" in msg
    ok, msg = await check({"GITHUB_TOKEN": "good", "GITHUB_API_URL": "https://down/api/v3/"})
    assert not ok and "unreachable" in msg and calls[-1][0] == "https://down/api/v3"
