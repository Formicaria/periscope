"""Log buffer + SSE tail, the first-run flow, restart, and the shared layout coroutines on a fake guild."""

from __future__ import annotations

import asyncio
import logging

import pytest
from periscope.layout import apply_git_layout, ensure_layout, git_env_lines, layout_status
from periscope_web import restart
from periscope_web.logs import LogBuffer

HX = {"HX-Request": "true"}


# ----- logs -------------------------------------------------------------------------------------------------
@pytest.fixture
def capture(logbuf):
    """Attach the ring buffer to the root logger at INFO for one test."""
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.INFO)
    root.addHandler(logbuf)
    yield logbuf
    root.removeHandler(logbuf)
    root.setLevel(old)


async def test_log_buffer_and_page_filter(client, logbuf, capture):
    logging.getLogger("periscope_pve.client").info("pve poll ok")
    logging.getLogger("periscope.presence").warning("[default] reconnecting")
    logging.getLogger("periscope_sonarr").error("sonarr unreachable")
    assert [line.level for line in logbuf.snapshot()] == ["INFO", "WARNING", "ERROR"] and logbuf.last_seq() == 3
    r = await client.get("/logs")
    assert r.status_code == 200 and "pve poll ok" in r.text and "[default] reconnecting" in r.text and 'data-since="3"' in r.text
    assert ">pve<" in r.text and ">[default]<" in r.text                                  # filter chips
    r = await client.get("/logs?q=sonarr")
    assert "sonarr unreachable" in r.text and "pve poll ok" not in r.text
    r = await client.get("/logs?level=WARNING")
    assert "pve poll ok" not in r.text and "reconnecting" in r.text and "sonarr unreachable" in r.text
    r = await client.get("/logs/download?q=pve")
    assert r.headers["content-type"].startswith("text/plain") and r.text.strip().endswith("pve poll ok")


async def test_log_stream_sse(client, logbuf, capture):
    logbuf.bind(asyncio.get_running_loop())
    logging.getLogger("periscope_pve").info("first")
    logging.getLogger("periscope_pve").info("second")

    async def later():
        await asyncio.sleep(0.05)
        logging.getLogger("periscope_pve").warning("third live")
        logging.getLogger("periscope_other").warning("not for us")
        logging.getLogger("periscope_pve").warning("fourth live")

    task = asyncio.create_task(later())
    r = await client.get("/logs/stream?since=0&q=pve&max=4")
    await task
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    frames = [f for f in r.text.split("\n\n") if f.strip()]
    assert len(frames) == 4
    assert frames[0].startswith("id: 1\nevent: log\ndata: ") and "first" in frames[0]
    assert "third live" in frames[2] and "fourth live" in frames[3] and "not for us" not in r.text
    # since= skips what the page already rendered
    r = await client.get("/logs/stream?since=1&q=pve&max=1")
    assert "second" in r.text and "first" not in r.text


def test_log_buffer_ring_and_keepalive():
    buf = LogBuffer(maxlen=3)
    for i in range(5):
        buf.emit(logging.LogRecord("x", logging.INFO, "f", 1, f"m{i}", None, None))
    assert [line.text.rsplit(" ", 1)[-1] for line in buf.snapshot()] == ["m2", "m3", "m4"] and buf.last_seq() == 5

    async def run():
        out = []
        async for item in buf.stream(since=5, keepalive_s=0.01, limit=1):
            out.append(item)
            if item is None:
                buf.emit(logging.LogRecord("x", logging.INFO, "f", 1, "late", None, None))
        return out

    out = asyncio.run(run())
    assert out[0] is None and out[-1] is not None and out[-1].text.endswith("late")


# ----- setup flow ------------------------------------------------------------------------------------------
@pytest.fixture
def fresh(store):
    """A store as it looks on a brand-new install: no token, no server."""
    store.presences["default"]["token"] = ""
    store.presences.pop("arr")
    store.lab["guild_id"] = ""
    store.services.clear()
    store.save()
    return store


async def test_overview_redirects_to_setup_without_token(client, fresh, app):
    app.state.runtime.presences.clear()
    r = await client.get("/")
    assert r.status_code == 302 and r.headers["location"] == "/setup"
    r = await client.get("/setup")
    assert r.status_code == 200 and "Bot token" in r.text and "set a bot token first" in r.text


async def test_setup_step1_token_and_step2_guild(client, fresh, reload, app, api_calls):
    app.state.runtime.presences.clear()
    r = await client.post("/setup/token", data={"token": "bad-token", "presence": "default"}, headers=HX)
    assert r.status_code == 422 and "rejected" in r.text and reload().presences["default"]["token"] == ""
    r = await client.post("/setup/token", data={"token": "good-token-abc", "presence": "default"}, headers=HX)
    assert r.status_code == 200 and "signed in as periscope" in r.text
    assert "client_id=777" in r.text and "permissions=268659728" in r.text               # invite link
    assert reload().presences["default"]["token"] == "good-token-abc" and "good-token-abc" not in r.text
    assert "THE LAB" in r.text and 'value="42"' in r.text and "Other" in r.text          # step 2 lists the bot's servers
    r = await client.post("/setup/guild", data={"guild_id": "42", "guild_name": "THE LAB"}, headers=HX)
    assert r.status_code == 200
    s = reload()
    assert s.lab["guild_id"] == "42" and s.lab["name"] == "testlab"                       # existing lab name kept
    assert "#lab-status" in r.text and "Create missing channels" in r.text               # step 3 via REST listing (mock)
    assert ("GET", "/api/v10/guilds/42/channels", "Bot") in api_calls
    assert "/services/pve" in r.text                                                     # step 4 links


async def test_setup_step3_layout_fills_lab_defaults(client, store, reload, guild):
    store.lab.update({"status_channel_id": "", "alert_channel_id": "", "alert_role_id": "", "admin_role_ids": []})
    r = await client.post("/setup/layout", headers=HX)
    assert r.status_code == 200 and "created #media" in r.text
    lab = reload().lab
    assert lab["status_channel_id"] == "1001" and lab["alert_channel_id"] == "1002" and lab["admin_role_ids"] == ["2001"]
    oncall = next(r.id for r in guild.roles if r.name == "lab-oncall")
    assert lab["alert_role_id"] == str(oncall)


# ----- restart --------------------------------------------------------------------------------------------
async def test_restart_schedules_exec(client, monkeypatch):
    calls = []
    monkeypatch.setattr(restart.os, "execv", lambda path, argv: calls.append((path, argv)))
    r = await client.post("/restart", headers=HX)
    assert r.status_code == 200 and "Restarting periscope" in r.text
    await asyncio.sleep(0.05)
    assert calls and calls[0][0] == restart.sys.executable and calls[0][1][0] == restart.sys.executable


def test_command_line_round_trips_module_invocation(monkeypatch):
    monkeypatch.setattr(restart.sys, "orig_argv", ["python3", "-m", "periscope"], raising=False)
    assert restart.command_line() == [restart.sys.executable, "-m", "periscope"]


# ----- core layout helpers -----------------------------------------------------------------------------------
def test_layout_status_lists_missing():
    st = layout_status(["lab-status", "GIT-anthill", "op-x"], ["bots"], github=True)
    assert st["missing_channels"] == ["lab-alerts", "media", "network", "backups", "lab-cmd", "formicaria-git", "formicaria-ci"]
    assert st["missing_roles"] == ["lab-admin", "lab-oncall", "formicaria-dev"] and st["git"] == ["git-anthill"] and st["op"] == ["op-x"]


async def test_ensure_layout_idempotent(guild):
    rep = await ensure_layout(guild)
    assert rep.changed and set(rep.created_channels) == {"media", "network", "backups", "lab-cmd"} and rep.created_roles == ["lab-oncall"]
    assert rep.created_categories == ["🕹️ LAB CONTROL"] and not rep.errors
    media = next(c for c in guild.text_channels if c.name == "media")
    assert media.category.name == "🧪 LAB STATUS"                                        # existing category reused
    again = await ensure_layout(guild)
    assert not again.changed and again.lines == ["nothing to create — the layout is complete"]


async def test_apply_git_layout_and_env_lines(guild):
    res = await apply_git_layout(guild, me_id=999, maps={"git-anthill": ["Anthill", "micro*"], "git-nope": ["x"]})
    assert res.channel_ids == {"git-anthill": 1003, "op-anthill": 1004} and not res.aborted
    assert guild.members[999].added and guild.members[999].added[0].name == "bots"     # the bot got @bots
    lines = git_env_lines(res, {"git-anthill": ["Anthill", "micro*"], "git-nope": ["x"]})
    assert lines[0] == "# !! no channel named #git-nope" and lines[1] == "GITHUB_REPO_CHANNEL_MAP=Anthill=1003,micro*=1003"
    guild.roles = [r for r in guild.roles if r.name != "bots"]
    res = await apply_git_layout(guild, me_id=999)
    assert res.aborted and res.errors
