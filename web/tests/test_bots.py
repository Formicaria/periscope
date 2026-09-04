"""The Bots page, laid out one section per Discord server: which bots a section holds, a bot that posts in two
servers appearing in both, the bots nothing uses yet, whether a bot is in that server, and the HTMX swaps."""

from __future__ import annotations

HX = {"HX-Request": "true"}
GUILD2_ID = 77                                   # the `plex` server in the fixtures


def section(html: str, slug: str) -> str:
    """One section's markup: from its wrapper id up to the next section."""
    return html.split(f'id="bots-{slug}"')[1].split("<section")[0]


# ----- the sections ------------------------------------------------------------------------------------------
async def test_a_section_per_server_holds_the_bots_that_post_in_it(client):
    r = await client.get("/presences")
    assert r.status_code == 200
    html = r.text
    assert html.index('id="bots-main"') < html.index('id="bots-plex"')                # the default server first
    main, plex = section(html, "main"), section(html, "plex")
    assert "testlab (THE LAB)" in main and ">42<" in main and 'href="/discord"' in main
    assert ">default<" in main                                                        # the default server says so
    assert "Plex land" in plex and ">77<" in plex
    # every service posts in the default server, so both bots sit there and the other section is empty
    assert 'id="presence-main-default"' in main and 'id="presence-main-arr"' in main
    assert ">pve<" in main and ">github<" in main and ">sonarr<" in main
    assert "presence-plex-" not in plex and "No bot posts in this server yet" in plex
    assert "bots-unused" not in html                                                  # every bot is in use


async def test_sections_follow_the_default_server_and_name_a_server_without_an_id(client, store):
    """The default server heads the page even when it is not the first block, and one without a Discord id
    still gets its own section."""
    store.servers["main"]["guild_id"] = ""                                            # → plex becomes the default
    store.save()
    html = (await client.get("/presences")).text
    assert html.index('id="bots-plex"') < html.index('id="bots-main"')
    assert "no server id yet" in section(html, "main") and ">default<" in section(html, "plex")
    assert 'id="presence-plex-default"' in section(html, "plex")                      # the services moved with it


async def test_a_bot_that_posts_in_two_servers_is_listed_in_both(client, store):
    store.services["github"]["server"] = "plex"
    store.save()
    html = (await client.get("/presences")).text
    main, plex = section(html, "main"), section(html, "plex")
    assert 'id="presence-main-default"' in main and 'id="presence-plex-default"' in plex
    assert ">pve<" in main and ">github<" not in main                                 # only the services of that server
    assert ">github<" in plex and ">pve<" not in plex
    assert "also posts in Plex land" in main and "also posts in testlab (THE LAB)" in plex
    assert 'id="presence-main-arr"' in main and "presence-plex-arr" not in plex       # sonarr stayed behind


async def test_a_bot_nothing_uses_lands_in_the_last_section(client, store, reload):
    r = await client.post("/presences", data={"name": "spare", "label": "Spare"}, headers=HX)
    assert r.status_code == 200 and "spare" in reload().presences
    html = (await client.get("/presences")).text
    assert html.index('id="bots-unused"') > html.index('id="bots-plex"')              # last, after the servers
    unused = section(html, "unused")
    assert 'id="presence-unused-spare"' in unused and "Not in use" in unused
    assert "pick this bot on a service's settings page" in unused
    assert "presence-main-spare" not in html and "presence-unused-default" not in html
    store.services["sonarr"]["presence"] = "spare"                                    # once a service picks it…
    store.save()
    html = (await client.get("/presences")).text
    assert 'id="presence-main-spare"' in section(html, "main")                         # …it moves to that service's server
    assert 'id="presence-unused-arr"' in section(html, "unused")                       # and the bot it replaced takes its place


# ----- is the bot in that server? ----------------------------------------------------------------------------
async def test_a_row_says_when_the_bot_is_not_in_that_server(client, store, app):
    store.services["github"]["server"] = "plex"
    store.save()
    app.state.runtime.presences["default"].missing_guilds = {GUILD2_ID: "github"}
    html = (await client.get("/presences")).text
    main, plex = section(html, "main"), section(html, "plex")
    assert "not in this server (github needs it)" in plex
    assert "https://discord.com/oauth2/authorize?client_id=999" in plex               # the invite link, in the row
    assert "in this server" in main and "not in this server" not in main


async def test_a_bot_without_a_token_claims_nothing_about_membership(client):
    html = (await client.get("/presences")).text
    row = section(html, "main").split('id="presence-main-arr"')[1]
    assert "in this server" not in row and "needs a token" in row


# ----- the swaps ---------------------------------------------------------------------------------------------
async def test_every_mutating_endpoint_returns_the_whole_grouped_list(client, store):
    """One bot can sit in several sections, so add/token/rename/remove swap #bots — only the invite link is
    still fetched per row."""
    store.services["github"]["server"] = "plex"
    store.save()
    html = (await client.get("/presences")).text
    for target in ('hx-post="/presences" hx-target="#bots"', 'hx-post="/presences/default/token" hx-target="#bots"',
                   'hx-post="/presences/default/label" hx-target="#bots"',
                   'hx-post="/presences/arr/delete" hx-target="#bots"'):
        assert target in html, target
    assert 'hx-get="/presences/default/invite"' in html                               # the invite is still per row…
    assert 'id="invite-main-default"' in html and 'id="invite-plex-default"' in html  # …once per section it is in
    assert 'hx-target="#presence-' not in html and "presence-table" not in html
    for path, data in (("/presences", {"name": "extra"}), ("/presences/extra/token", {"token": "good-token-abc"}),
                       ("/presences/extra/label", {"label": "Extra", "new_name": "extra"}),
                       ("/presences/extra/delete", {})):
        r = await client.post(path, data=data, headers=HX)
        assert r.status_code == 200, path
        assert r.text.startswith('<div id="bots"'), path
        assert 'id="bots-main"' in r.text and 'id="bots-plex"' in r.text, path
