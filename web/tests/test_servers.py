"""The Discord servers page: several servers on one page, add/remove/default, per-server pickers, a service's
own server, and the wording sweep (no page still talks about a lab)."""

from __future__ import annotations

import re
from pathlib import Path

import periscope_web

HX = {"HX-Request": "true"}
GUILD2_ID = 77                                   # the `plex` server in the fixtures
TEMPLATES = Path(periscope_web.__file__).parent / "templates"

# the literal Discord names `periscope layout` creates, plus the env keys the bots read: those keep saying lab
ALLOWED = ("lab-status", "lab-alerts", "lab-cmd", "lab-admin", "lab-oncall", "LAB STATUS", "LAB CONTROL",
           "LAB_NAME", "LAB_COLOR")


# ----- the page ---------------------------------------------------------------------------------------------
async def test_page_lists_every_server_with_its_own_pickers(client):
    r = await client.get("/discord")
    assert r.status_code == 200
    html = r.text
    assert 'id="server-main"' in html and 'id="server-plex"' in html                 # a card each
    assert "testlab" in html and "Plex land" in html
    assert ">default<" in html and "make default" in html                            # main is the default one
    # each card offers the channels/roles of its own server
    main = html.split('id="server-main"')[1].split('id="server-plex"')[0]
    plex = html.split('id="server-plex"')[1].split('id="server-add"')[0]
    assert "#lab-alerts" in main and "@lab-admin" in main and "#plex-alerts" not in main
    assert "#plex-alerts" in plex and "@plex-admin" in plex and "#lab-alerts" not in plex
    assert '<option value="3002" selected>' in plex                                  # its stored alert channel
    assert 'name="status_interval_s"' in html and 'name="log_level"' in html         # the shared settings card
    assert "Web sign-in (Discord OAuth2)" in html and "Channel layout" in html


async def test_save_one_server_and_validate(client, reload):
    r = await client.post("/discord/servers/plex", data={"name": "Plexes", "color": "#00ff00", "guild_id": str(GUILD2_ID),
                                                         "status_channel_id": "3001", "alert_channel_id": "3003",
                                                         "alert_role_id": "4001", "admin_role_ids": ["4001"]}, headers=HX)
    assert r.status_code == 200 and 'id="server-plex"' in r.text and "restart to apply" in r.text
    s = reload()
    assert s.servers["plex"] == {"name": "Plexes", "color": "00FF00", "guild_id": "77", "status_channel_id": "3001",
                                 "alert_channel_id": "3003", "alert_role_id": "4001", "admin_role_ids": ["4001"]}
    assert s.servers["main"]["name"] == "testlab"                                    # the other card untouched
    r = await client.post("/discord/servers/plex", data={"color": "zz", "guild_id": "nope",
                                                         "alert_channel_id": "x"}, headers=HX)
    assert r.status_code == 422
    assert "color must be 6 hex digits" in r.text and "server id must be a Discord id" in r.text
    assert "alert_channel_id must be a Discord id" in r.text
    assert reload().servers["plex"]["color"] == "00FF00"                             # nothing written
    r = await client.post("/discord/servers/ghost", data={"name": "x"}, headers=HX)
    assert r.status_code == 404


async def test_add_and_remove_a_server_reports_moved_services(client, store, reload):
    store.remove_server("plex")                                                      # the bot is still in it
    store.save()
    r = await client.get("/discord")
    assert "Plex land · 77" in r.text                                                # offered: a server the bot is in
    assert "THE LAB · 42" not in r.text                                              # not offered: already on the page
    r = await client.post("/discord/servers", data={"pick": str(GUILD2_ID)}, headers=HX)   # picked, so the name is Discord's
    assert r.status_code == 200 and reload().servers["plex-land"]["name"] == "Plex land"
    assert "#plex-alerts" in r.text.split('id="server-plex-land"')[1]                # its pickers work straight away
    r = await client.post("/discord/servers", data={"name": "Guild hall", "guild_id": "12345"}, headers=HX)
    assert r.status_code == 200 and 'id="server-guild-hall"' in r.text
    s = reload()
    assert s.servers["guild-hall"]["name"] == "Guild hall" and s.servers["guild-hall"]["guild_id"] == "12345"
    # that server has no bot in it: plain id inputs plus the invite link
    card = r.text.split('id="server-guild-hall"')[1]
    assert "The bot is not in this server" in card and 'name="alert_channel_id"' in card
    assert "https://discord.com/oauth2/authorize?client_id=999" in card
    r = await client.post("/discord/servers", data={"guild_id": "12345"}, headers=HX)
    assert r.status_code == 422 and "already on this page" in r.text and "guild-hall-2" not in r.text
    r = await client.post("/discord/servers", data={"guild_id": "abc"}, headers=HX)
    assert r.status_code == 422 and "must be a Discord id" in r.text
    r = await client.post("/discord/servers", data={}, headers=HX)
    assert r.status_code == 422 and "give the server a name" in r.text
    # a service posting in the removed server falls back to the default one
    await client.post("/services/sonarr", data={"_server": "guild-hall", "SONARR_URL": "https://s"}, headers=HX)
    assert reload().services["sonarr"]["server"] == "guild-hall"
    r = await client.post("/discord/servers/guild-hall/delete", headers=HX)
    assert r.status_code == 200 and "sonarr now post in testlab" in r.text
    s = reload()
    assert "guild-hall" not in s.servers and s.services["sonarr"]["server"] == "main"
    r = await client.post("/discord/servers/nope/delete", headers=HX)
    assert r.status_code == 404


async def test_last_server_cannot_be_removed(client, store, reload):
    store.remove_server("plex")
    store.save()
    r = await client.post("/discord/servers/main/delete", headers=HX)
    assert r.status_code == 422 and "only server" in r.text and "main" in reload().servers


async def test_marking_a_server_default(client, reload):
    """The two blocks swap places (a reload always sorts `main` first), and every service keeps posting where
    it posted before — only new ones follow the new default."""
    r = await client.post("/discord/servers/plex/default", headers=HX)
    assert r.status_code == 200 and "Plex land is now the default" in r.text
    s = reload()
    assert s.server(s.default_server())["name"] == "Plex land"
    assert s.server()["guild_id"] == "77" and s.servers["plex"]["name"] == "testlab"
    assert s.env_for("pve")["GUILD_ID"] == "42" and s.env_for("pve")["LAB_NAME"] == "testlab"   # pve did not move
    assert s.service("newcomer")["server"] == s.default_server()                     # a new service posts in Plex land
    # a server without an id cannot be the default: nothing would work in it
    await client.post("/discord/servers", data={"name": "Empty"}, headers=HX)
    r = await client.post("/discord/servers/empty/default", headers=HX)
    assert r.status_code == 422 and "Discord server id first" in r.text
    assert reload().server()["name"] == "Plex land"


async def test_globals_saved_apart_from_the_servers(client, reload):
    r = await client.post("/discord/globals", data={"log_level": "warning", "status_interval_s": "45"}, headers=HX)
    assert r.status_code == 200
    s = reload()
    assert s.globals == {"log_level": "WARNING", "status_interval_s": 45}
    assert "log_level" not in s.servers["main"] and "status_interval_s" not in s.servers["main"]
    r = await client.post("/discord/globals", data={"log_level": "INFO", "status_interval_s": "soon"}, headers=HX)
    assert r.status_code == 422 and "whole number of seconds" in r.text
    assert reload().globals["status_interval_s"] == 45


# ----- pickers without a connected bot ------------------------------------------------------------------------
async def test_pickers_fall_back_to_rest_per_server(client, app, api_calls):
    """No presence connected → each card lists its own server over the REST API, with any bot token."""
    app.state.runtime.presences.clear()
    app.state.guild.invalidate()
    r = await client.get("/discord")
    assert r.status_code == 200
    plex = r.text.split('id="server-plex"')[1]
    assert "#plex-status" in plex and "@plex-admin" in plex
    paths = [p for _m, p, _a in api_calls]
    assert "/api/v10/guilds/42/channels" in paths and "/api/v10/guilds/77/channels" in paths
    assert "/api/v10/guilds/42/roles" in paths and "/api/v10/guilds/77/roles" in paths


async def test_layout_panel_acts_on_the_chosen_server(client, guild, guild2):
    r = await client.get("/discord/layout?server=plex")
    assert r.status_code == 200 and 'value="plex" selected' in r.text                 # the picker follows the choice
    assert "#lab-status</span>" in r.text and "missing" in r.text                     # nothing of the convention there yet
    r = await client.post("/discord/layout/create", data={"server": "plex"}, headers=HX)
    assert r.status_code == 200 and "created #lab-status" in r.text
    assert {"lab-status", "lab-alerts", "lab-cmd"} <= {n for _k, n in guild2.created}  # created in the second server
    assert not guild.created                                                          # and not in the default one


# ----- a service picks its server -------------------------------------------------------------------------------
async def test_service_form_saves_its_server_and_uses_it(client, reload):
    r = await client.get("/services/pve")
    assert 'name="_server"' in r.text and ">Plex land</option>" in r.text
    assert '<option value="main" selected>testlab</option>' in r.text
    assert "server default: #lab-alerts" in r.text                                   # main's alert channel
    r = await client.post("/services/pve", data={"_enabled": "true", "_server": "plex", "PVE_URL": "https://pve:8006",
                                                 "PVE_TOKEN_SECRET": ""}, headers=HX)
    assert r.status_code == 200
    s = reload()
    assert s.services["pve"]["server"] == "plex" and s.server_for("pve") == "plex"
    assert s.env_for("pve")["GUILD_ID"] == "77" and s.env_for("pve")["ALERT_CHANNEL_ID"] == "3002"
    assert s.env_for("pve")["LAB_NAME"] == "Plex land"
    r = await client.get("/services/pve")
    assert "#plex-alerts" in r.text and "#lab-alerts" not in r.text                   # pickers followed the service
    assert "server default: #plex-alerts" in r.text
    r = await client.post("/services/pve", data={"_server": "ghost", "PVE_URL": "https://x"}, headers=HX)
    assert r.status_code == 422 and "unknown server" in r.text


async def test_overview_card_and_routing_row_name_the_server(client, store):
    store.services["pve"]["server"] = "plex"
    store.save()
    r = await client.get("/")
    card = r.text.split('id="card-pve"')[1].split('id="card-')[0]
    assert "posts as" in card and "Plex land" in card
    r = await client.get("/routing")
    row = r.text.split('id="alert-pve"')[1].split("</tr>")[0]
    assert "Plex land" in row and "server default · #plex-alerts" in row              # its own server's defaults
    assert "#requests" in row and "#lab-alerts" not in row                            # and its own channels
    sonarr = r.text.split('id="alert-sonarr"')[1].split("</tr>")[0]
    assert "#lab-alerts" in sonarr                                                    # still on the default server


async def test_single_server_install_says_nothing_about_servers(client, store):
    store.remove_server("plex")
    store.save()
    r = await client.get("/")
    assert "posts as" in r.text and " in <a href=\"/discord\"" not in r.text
    r = await client.get("/routing")
    assert "server default · #lab-alerts" in r.text and "badge-outline badge-xs" not in r.text


# ----- wording ----------------------------------------------------------------------------------------------
def test_no_template_still_talks_about_a_lab():
    """Every user-facing string is server-centric now; only the convention channel/role names and the LAB_* env
    keys the bots read may still say lab."""
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for token in ALLOWED:
            text = text.replace(token, "")
        if re.search(r"\blab\b", text, re.IGNORECASE):
            offenders.append(path.name)
    assert offenders == []


async def test_pages_render_without_the_word_lab(client):
    """The same sweep on rendered pages. /messages is left out: its cards are the sample posts the installed
    bots ship, which are not ours to reword."""
    for path in ("/", "/discord", "/routing", "/services/pve", "/setup", "/presences"):
        r = await client.get(path)
        assert r.status_code == 200, path
        text = r.text
        for token in (*ALLOWED, "THE LAB", "testlab"):     # the fake guild and server names are test data
            text = text.replace(token, "")
        assert not re.search(r"\blab\b", text, re.IGNORECASE), path


async def test_cards_say_when_no_bot_has_a_token(client, store, app):
    store.presences["default"]["token"] = ""
    store.save()
    app.state.runtime.presences.clear()
    app.state.guild.invalidate()
    r = await client.get("/discord")
    assert r.status_code == 200 and r.text.count("No bot has a token yet") == 2      # one line per card
    assert 'name="alert_channel_id"' in r.text and "<select name=\"alert_channel_id\"" not in r.text


# ----- a bot that is not in one of the servers -------------------------------------------------------------------
async def test_card_says_when_the_bot_is_not_in_that_server(client, app, store):
    """A server nobody's bot is in: plain id inputs, a line saying so, and the invite link from the runtime."""
    store.add_server("far", "Far away")["guild_id"] = "5150"
    store.save()
    app.state.runtime.presences["default"].missing_guilds = {5150: "pve"}
    app.state.guild.invalidate()
    r = await client.get("/discord")
    card = r.text.split('id="server-far"')[1]
    assert 'name="status_channel_id"' in card and "<select" not in card.split('name="admin_role_ids"')[0]
    assert "The bot is not in this server" in card and "client_id=999" in card
