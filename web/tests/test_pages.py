"""Overview cards, the service settings form round trip, Test, presences, /discord, routing, JSON API."""

from __future__ import annotations

from periscope_web.discordapi import INVITE_PERMS

HX = {"HX-Request": "true"}


# ----- overview -------------------------------------------------------------------------------------------
async def test_overview_cards_and_states(client):
    r = await client.get("/")
    assert r.status_code == 200
    html = r.text
    assert 'id="card-pve"' in html and 'id="card-sonarr"' in html and 'id="card-github"' in html
    assert ">running<" in html and ">needs setup<" in html and ">off<" in html
    assert "needs Github Org" in html and "Needs attention" in html and 'href="/services/github"' in html   # the problem + where to fix it
    assert "Infrastructure" in html and "Media" in html and "Dev" in html
    assert "periscope#0001" in html                           # bot label / connected user
    assert "restart to apply" not in html                     # store untouched since start
    assert "Restart</button>" not in html                     # no per-card restart: one button in the header when dirty


async def test_enable_disable_cards_update_store_and_flag_restart(client, reload, app, store):
    # switching on a service whose required settings are empty goes to its settings page instead
    r = await client.post("/services/sonarr/enable", headers=HX)
    assert r.status_code == 200 and r.headers.get("HX-Redirect") == "/services/sonarr"
    assert reload().services["sonarr"]["enabled"] is False
    store.update_service_env("sonarr", {"SONARR_URL": "https://s", "SONARR_API_KEY": "k"})
    store.presences["arr"]["token"] = "good-token-abc"
    store.save()
    r = await client.post("/services/sonarr/enable", headers=HX)
    assert r.status_code == 200 and 'id="card-sonarr"' in r.text and "Switch off" in r.text
    assert "next restart" in r.text                           # OOB toast: on, starts on restart
    assert reload().services["sonarr"]["enabled"] is True and app.state.dirty()
    r = await client.post("/services/pve/disable", headers=HX)
    assert r.status_code == 200 and "Switch on" in r.text
    assert reload().services["pve"]["enabled"] is False
    r = await client.get("/")
    assert "restart to apply" in r.text
    r = await client.post("/services/nope/enable", headers=HX)
    assert r.status_code == 404


async def test_check_uses_stored_env_from_overview(client, monkeypatch, app):
    seen = {}

    async def fake_check(env):
        seen.update(env)
        return True, "fine"

    monkeypatch.setattr(app.state.runtime.specs["pve"], "check", fake_check)
    r = await client.post("/services/pve/check", headers=HX)
    assert r.status_code == 200 and "fine" in r.text and "alert-success" in r.text
    assert seen["PVE_URL"] == "https://pve:8006" and seen["PVE_TOKEN_SECRET"] == "s3cret" and seen["DISCORD_TOKEN"] == "good-token-abc"
    r = await client.post("/services/sonarr/check", headers=HX)   # no check() on this spec
    assert r.status_code == 200 and "no credential check" in r.text


# ----- service form ---------------------------------------------------------------------------------------
async def test_service_form_renders_typed_fields(client):
    r = await client.get("/services/pve")
    assert r.status_code == 200
    html = r.text
    assert 'type="password"' in html and "s3cret" not in html                       # secret never rendered
    assert "•••• set" in html and 'name="clear_PVE_TOKEN_SECRET"' in html
    assert 'type="checkbox" id="f-PVE_VERIFY_SSL"' in html                           # bool → toggle
    assert 'type="number" id="f-PVE_CPU_WARN"' in html and 'value="90"' in html      # int, stored override
    assert '<select id="f-MEDIA_CHANNEL_ID"' in html and "#lab-alerts" in html      # channel picker from the fake guild
    assert '<select id="f-PVE_ROLE_ID"' in html and "@lab-admin" in html            # role picker
    assert '<select id="f-PVE_MODE"' in html and 'value="fast"' in html             # choice
    assert "Proxmox VE" in html and "Thresholds" in html and "Discord routing" in html
    assert 'title="required"' in html and "API base URL" in html                     # required marker + help
    assert "lab default: #lab-alerts" in html                                        # shared key hint
    r = await client.get("/services/nope")
    assert r.status_code == 404


async def test_service_save_round_trip_keeps_blank_secret(client, reload):
    form = {"_enabled": "true", "_presence": "default", "PVE_URL": "https://new:8006", "PVE_TOKEN_SECRET": "", "PVE_CPU_WARN": "70",
            "PVE_VERIFY_SSL": "true", "PVE_MODE": "fast", "MEDIA_CHANNEL_ID": "1002", "PVE_ROLE_ID": "", "PVE_TAGS": "a, b",
            "STATUS_CHANNEL_ID": "", "ALERT_CHANNEL_ID": "1001", "ALERT_ROLE_ID": "", "STATUS_INTERVAL_S": ""}
    r = await client.post("/services/pve", data=form, headers=HX)
    assert r.status_code == 200 and "saved" in r.text
    svc = reload().services["pve"]
    env = svc["env"]
    assert env["PVE_URL"] == "https://new:8006" and env["PVE_TOKEN_SECRET"] == "s3cret"      # blank secret kept
    assert env["PVE_CPU_WARN"] == "70" and env["PVE_VERIFY_SSL"] == "true" and env["PVE_MODE"] == "fast"
    assert env["MEDIA_CHANNEL_ID"] == "1002" and env["ALERT_CHANNEL_ID"] == "1001" and "PVE_ROLE_ID" not in env
    assert svc["enabled"] is True and svc["presence"] == "default"
    # new secret overwrites, clear removes, disabling + switching presence works, non-HTMX redirects
    form.update({"PVE_TOKEN_SECRET": "newsecret", "_presence": "arr"})
    del form["_enabled"]
    r = await client.post("/services/pve", data=form)
    assert r.status_code == 303 and r.headers["location"] == "/services/pve"
    svc = reload().services["pve"]
    assert svc["env"]["PVE_TOKEN_SECRET"] == "newsecret" and svc["enabled"] is False and svc["presence"] == "arr"
    form.update({"PVE_TOKEN_SECRET": "", "clear_PVE_TOKEN_SECRET": "true"})
    r = await client.post("/services/pve", data=form, headers=HX)
    assert r.status_code == 200 and "PVE_TOKEN_SECRET" not in reload().services["pve"]["env"]


async def test_service_save_validates(client, reload):
    r = await client.post("/services/pve", data={"_enabled": "true", "PVE_URL": "", "PVE_CPU_WARN": "lots", "MEDIA_CHANNEL_ID": "abc"}, headers=HX)
    assert r.status_code == 422
    assert "must be a whole number" in r.text and "Pve Url is required" in r.text and "must be a Discord id" in r.text
    assert reload().services["pve"]["env"]["PVE_CPU_WARN"] == "90"                    # nothing written
    # a disabled service may be saved incomplete (configure incrementally); the toast says what is still needed
    r = await client.post("/services/pve", data={"PVE_URL": "", "PVE_CPU_WARN": "80"}, headers=HX)
    assert r.status_code == 200 and "still needed before switching on: Pve Url" in r.text
    assert reload().services["pve"]["env"]["PVE_CPU_WARN"] == "80"
    r = await client.post("/services/pve", data={"PVE_URL": "https://x", "_presence": "ghost"}, headers=HX)
    assert r.status_code == 422 and "unknown bot" in r.text


async def test_check_with_submitted_values_does_not_save(client, monkeypatch, app, reload):
    seen = {}

    async def fake_check(env):
        seen.update(env)
        return False, "nope"

    monkeypatch.setattr(app.state.runtime.specs["pve"], "check", fake_check)
    r = await client.post("/services/pve/check", data={"_enabled": "true", "PVE_URL": "https://try:8006", "PVE_TOKEN_SECRET": "",
                                                       "PVE_CPU_WARN": "50"}, headers=HX)
    assert r.status_code == 200 and "nope" in r.text and "alert-error" in r.text
    assert seen["PVE_URL"] == "https://try:8006" and seen["PVE_TOKEN_SECRET"] == "s3cret" and seen["PVE_CPU_WARN"] == "50"
    assert reload().services["pve"]["env"]["PVE_URL"] == "https://pve:8006"           # untouched
    r = await client.post("/services/pve/check", data={"PVE_URL": "https://try", "PVE_CPU_WARN": "x"}, headers=HX)
    assert "must be a whole number" in r.text


# ----- presences ----------------------------------------------------------------------------------------
async def test_presences_page_add_token_invite(client, reload, api_calls):
    r = await client.get("/presences")
    assert r.status_code == 200 and "good-token-abc" not in r.text
    assert ">set<" in r.text and ">missing<" in r.text and "online" in r.text and "periscope#0001" in r.text
    assert 'mono mr-1" title="switched on">pve<' in r.text                              # services using the bot
    r = await client.post("/presences", data={"name": "Bad Name"}, headers=HX)
    assert r.status_code == 422
    r = await client.post("/presences", data={"name": "plex", "label": "Plex bot"}, headers=HX)
    assert r.status_code == 200 and 'id="presence-plex"' in r.text
    assert reload().presences["plex"] == {"token": "", "label": "Plex bot"}
    r = await client.post("/presences/plex/token", data={"token": "bad-token"}, headers=HX)
    assert r.status_code == 422 and "rejected" in r.text and reload().presences["plex"]["token"] == ""
    r = await client.post("/presences/plex/token", data={"token": "good-token-abc"}, headers=HX)
    assert r.status_code == 200 and "token works" in r.text and "app id 777" in r.text
    assert reload().presences["plex"]["token"] == "good-token-abc" and "good-token-abc" not in r.text
    r = await client.get("/presences/plex/invite")
    assert "client_id=777" in r.text and f"permissions={INVITE_PERMS}" in r.text and "applications.commands" in r.text
    r = await client.get("/presences/default/invite")                                  # connected presence → no API call
    assert "client_id=999" in r.text
    assert ("GET", "/api/v10/users/@me", "Bot") in api_calls


async def test_presences_rename_and_remove(client, reload):
    r = await client.post("/presences/arr/label", data={"label": "Arr stack", "new_name": "media"}, headers=HX)
    assert r.status_code == 200
    s = reload()
    assert "arr" not in s.presences and s.presences["media"]["label"] == "Arr stack" and s.services["sonarr"]["presence"] == "media"
    r = await client.post("/presences/default/delete", headers=HX)
    assert r.status_code == 422 and "cannot be removed" in r.text                        # the only bot with a token
    r = await client.post("/presences/media/delete", headers=HX)
    assert r.status_code == 200
    s = reload()
    assert "media" not in s.presences and s.services["sonarr"]["presence"] == "default"


# ----- /discord ------------------------------------------------------------------------------------------
async def test_lab_page_and_save(client, reload):
    r = await client.get("/discord")
    assert r.status_code == 200
    assert "csecret" not in r.text and "•••• set" in r.text                               # OAuth secret masked
    assert "http://test/auth/callback" in r.text
    assert "#lab-status" in r.text and "#media" in r.text and "@lab-oncall" in r.text        # layout panel
    assert "git-anthill" in r.text and "op-anthill" in r.text
    r = await client.post("/discord", data={"name": "THE LAB", "color": "#5a189a", "guild_id": "42", "status_channel_id": "1001",
                                            "alert_channel_id": "1002", "alert_role_id": "2001", "admin_role_ids": ["2001", "2002"],
                                            "log_level": "debug", "status_interval_s": "30"}, headers=HX)
    assert r.status_code == 200
    lab = reload().lab
    assert lab["name"] == "THE LAB" and lab["color"] == "5A189A" and lab["admin_role_ids"] == ["2001", "2002"]
    assert lab["log_level"] == "DEBUG" and lab["status_interval_s"] == 30 and lab["alert_role_id"] == "2001"
    r = await client.post("/discord", data={"color": "zzz", "guild_id": "x"}, headers=HX)
    assert r.status_code == 422 and reload().lab["color"] == "5A189A"
    r = await client.post("/discord/web", data={"base_url": "https://p.example/", "oauth_client_id": "newid", "oauth_client_secret": "",
                                                "allowed_role_ids": "2002", "port": "8091"}, headers=HX)
    assert r.status_code == 200
    web = reload().web
    assert web["base_url"] == "https://p.example" and web["oauth_client_id"] == "newid" and web["oauth_client_secret"] == "csecret"
    assert web["allowed_role_ids"] == ["2002"] and web["port"] == 8091


async def test_layout_create_and_git_permissions_via_presence(client, guild):
    r = await client.post("/discord/layout/create", headers=HX)
    assert r.status_code == 200
    created = dict((k, n) for k, n in guild.created)
    names = [n for _, n in guild.created]
    assert {"media", "network", "backups", "lab-cmd"} <= set(names) and "lab-status" not in names   # existing kept
    assert "lab-oncall" in names and "lab-admin" not in names and "🕹️ LAB CONTROL" in names
    assert "created #media" in r.text and created  # report rendered
    r = await client.post("/discord/layout/git", headers=HX)
    assert r.status_code == 200 and "#git-anthill: feed" in r.text and "#op-anthill: discussion" in r.text
    git = next(c for c in guild.text_channels if c.name == "git-anthill")
    assert git.overwrites["@everyone"].send_messages is False and git.overwrites["bots"].send_messages is True
    op = next(c for c in guild.text_channels if c.name == "op-anthill")
    assert op.overwrites["bots"].send_messages is False
    assert "GITHUB_REPO_CHANNEL_MAP=" in r.text
    r = await client.post("/discord/layout/git", data={"dry": "1"}, headers=HX)
    assert "[dry-run]" in r.text


# ----- routing ----------------------------------------------------------------------------------------------
async def test_routing_page_and_save(client, reload):
    r = await client.get("/routing")
    assert r.status_code == 200
    assert 'value="Anthill"' in r.text and 'value="micro*"' in r.text and "#git-anthill" in r.text
    assert 'id="alert-pve"' in r.text and "lab default · #lab-alerts" in r.text
    r = await client.post("/routing", data={"repo": ["Anthill", "periscope", ""], "channel": ["1003", "1001", ""],
                                            "GITHUB_FEED_CHANNEL_ID": "1002", "GITHUB_CI_CHANNEL_ID": "", "GITHUB_MIRROR_TO_FEED": "true"}, headers=HX)
    assert r.status_code == 200 and "2 rule(s)" in r.text
    env = reload().services["github"]["env"]
    assert env["GITHUB_REPO_CHANNEL_MAP"] == "Anthill=1003,periscope=1001" and env["GITHUB_FEED_CHANNEL_ID"] == "1002"
    assert "GITHUB_CI_CHANNEL_ID" not in env and env["GITHUB_MIRROR_TO_FEED"] == "true"
    r = await client.post("/routing", data={"repo": ["x"], "channel": ["nope"]}, headers=HX)
    assert r.status_code == 422 and reload().services["github"]["env"]["GITHUB_REPO_CHANNEL_MAP"] == "Anthill=1003,periscope=1001"
    r = await client.get("/routing/row")
    assert 'name="repo"' in r.text and 'name="channel"' in r.text


async def test_alert_routing_inline_save(client, reload):
    r = await client.post("/routing/alerts/sonarr", data={"ALERT_CHANNEL_ID": "1002", "STATUS_CHANNEL_ID": "", "ALERT_ROLE_ID": "2001"}, headers=HX)
    assert r.status_code == 200 and 'id="alert-sonarr"' in r.text
    env = reload().services["sonarr"]["env"]
    assert env == {"ALERT_CHANNEL_ID": "1002", "ALERT_ROLE_ID": "2001"}
    r = await client.post("/routing/alerts/sonarr", data={"ALERT_CHANNEL_ID": "bad"}, headers=HX)
    assert r.status_code == 422


# ----- api ----------------------------------------------------------------------------------------------------
async def test_api_status_and_config_masked(client, store):
    r = await client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert data["services"]["pve"]["state"] == "running" and data["presences"]["default"]["connected"] is True
    assert data["restart_needed"] is False and data["pid"] == 1
    r = await client.get("/api/config")
    cfg = r.json()
    assert cfg["presences"]["default"]["token"] == "••••••••" and cfg["services"]["pve"]["env"]["PVE_TOKEN_SECRET"] == "••••••••"
    assert cfg["web"]["oauth_client_secret"] == "••••••••" and "good-token-abc" not in r.text and "s3cret" not in r.text
    assert cfg["services"]["pve"]["env"]["PVE_URL"] == "https://pve:8006"


async def test_no_secret_leaks_anywhere(client):
    for path in ("/", "/services/pve", "/services/github", "/presences", "/discord", "/routing", "/setup", "/logs"):
        r = await client.get(path)
        assert r.status_code == 200, path
        assert "good-token-abc" not in r.text and "s3cret" not in r.text and "csecret" not in r.text, path
