"""Dispatcher pipeline + webhook route, with a fake bot (no Discord, no network)."""

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from periscope import JsonState, WebhookServer

from periscope_github.config import GithubSettings
from periscope_github.dispatch import Dispatcher
from periscope_github.cogs.events import GithubEvents

REPO = {"name": "anthill", "full_name": "formicaria/anthill", "html_url": "https://github.com/formicaria/anthill",
        "default_branch": "main"}


class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []

    async def send(self, content=None, embed=None, **kw):
        self.sent.append((content, embed))
        return SimpleNamespace(id=1, channel=self, jump_url="https://discord/1")


class FakeAlerts:
    def __init__(self):
        self.fired, self.resolved = [], []

    async def fire(self, alert, force=False):
        self.fired.append(alert)
        return None

    async def resolve(self, fp, note=None):
        self.resolved.append(fp)
        return True


class FakeBot:
    def __init__(self, tmp_path, cfg):
        self.state = JsonState(tmp_path / "state.json")
        self.lab_name = "THE LAB"
        self.settings = SimpleNamespace(alert_channel_id=1, status_channel_id=None)
        self.alerts = FakeAlerts()
        self.channels = {1: FakeChannel(1), 123: FakeChannel(123)}
        self.gh_settings = cfg
        self.webhook = None

    async def get_channel_safe(self, cid):
        return self.channels.get(cid)


def star(login="alice"):
    return {"action": "created", "repository": REPO, "sender": {"login": login}}


def wf(conclusion, branch="main"):
    return {"action": "completed", "repository": REPO, "sender": {"login": "alice"},
            "workflow_run": {"name": "CI", "conclusion": conclusion, "head_branch": branch, "html_url": "https://run",
                             "head_sha": "abc1234567", "run_number": 1}}


@pytest.mark.asyncio
async def test_pipeline(tmp_path):
    cfg = GithubSettings(repo_channel_map={"anthill": 123})
    bot = FakeBot(tmp_path, cfg)
    d = Dispatcher(bot, cfg)
    assert await d.dispatch("star", star(), delivery_id="d1") is True
    assert await d.dispatch("star", star(), delivery_id="d1") is False  # dedupe
    assert await d.dispatch("star", star("dependabot[bot]"), delivery_id="d2") is True   # bots are shown by default
    d.cfg.ignore_bots = True
    assert await d.dispatch("star", star("dependabot[bot]"), delivery_id="d2b") is False  # ... unless filtered
    d.cfg.ignore_bots = False
    assert len(bot.channels[123].sent) == 2 and not bot.channels[1].sent  # mapped repo → its channel only
    assert d.activity_summary() == {"star": 2}
    assert len(d.recent()) == 2 and "starred by alice" in d.recent()[1]

    # CI: failure on default branch fires CRITICAL, success resolves, feature branch does nothing
    await d.dispatch("workflow_run", wf("failure"), delivery_id="w1")
    assert [a.fingerprint for a in bot.alerts.fired] == ["gh:anthill:workflow:CI:main"]
    assert bot.alerts.fired[0].severity.value == "critical"
    assert d.ci_status()["anthill"]["ok"] is False
    await d.dispatch("workflow_run", wf("success"), delivery_id="w2")
    assert bot.alerts.resolved == ["gh:anthill:workflow:CI:main"]
    assert d.ci_status()["anthill"]["ok"] is True
    await d.dispatch("workflow_run", wf("failure", branch="feat"), delivery_id="w3")
    assert len(bot.alerts.fired) == 1

    # event filter
    cfg2 = GithubSettings(events=["push"])
    d2 = Dispatcher(bot, cfg2)
    assert await d2.dispatch("star", star(), delivery_id="d9") is False

    # state persisted
    assert JsonState(tmp_path / "state.json").get("gh:activity")


@pytest.mark.asyncio
async def test_webhook_route(tmp_path):
    cfg = GithubSettings()
    bot = FakeBot(tmp_path, cfg)
    bot.webhook = WebhookServer("127.0.0.1", 0, secret="s3cret")
    GithubEvents(bot)  # registers POST /github
    async with TestClient(TestServer(bot.webhook.app)) as client:
        def post(event, payload, sign=True, delivery="x1"):
            body = json.dumps(payload).encode()
            headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "Content-Type": "application/json"}
            if sign:
                headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
            return client.post("/github", data=body, headers=headers)

        r = await post("star", star(), sign=False)
        assert r.status == 401
        r = await post("ping", {"zen": "Keep it logically awesome.", "hook": {"config": {"url": "https://h/github"}}})
        assert r.status == 200 and (await r.json())["pong"] is True
        r = await post("star", star(), delivery="s1")
        assert r.status == 200 and (await r.json())["posted"] is True
        r = await post("star", star(), delivery="s1")
        assert (await r.json())["posted"] is False  # duplicate delivery
        r = await post("gollum", {"pages": []})
        assert r.status == 200 and (await r.json())["posted"] is False   # verbose, but no repository → nothing to say
        r = await post("gollum", {"pages": [], "repository": star()["repository"], "sender": star()["sender"]}, delivery="g2")
        assert r.status == 200 and (await r.json())["posted"] is True    # verbose fallback card
        bot.gh_settings.verbose = False
        r = await post("gollum", {"pages": []}, delivery="g3")
        assert (await r.json())["ignored"] == "gollum"
        bot.gh_settings.verbose = True
        r = await client.post("/github", data=b"not json", headers={"X-GitHub-Event": "push", "X-Webhook-Secret": "s3cret"})
        assert r.status == 400
        r = await client.get("/health")
        assert r.status == 200
    assert len(bot.channels[1].sent) == 2   # the star + the verbose gollum card
