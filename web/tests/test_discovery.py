"""The /discover page: it renders, an admin can start a scan, and a finding can be applied.

The scanner itself is replaced with a function that returns a fixed list, so no test here opens a socket. The
router is mounted onto the app by hand — `routes/__init__.register()` is shared with the rest of the UI and is
not this module's to edit.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from periscope.discovery import Found

from periscope_web.auth import SESSION_COOKIE

SCANNED = [Found("sonarr", url="http://10.0.0.5:8989", host="10.0.0.5", port=8989, confidence="family",
                 note="answered 401 on its API, the way an *arr does"),
           Found("proxmox", url="https://10.0.0.1:8006", host="10.0.0.1", port=8006, version="8.2.2"),
           Found("netdata", url="http://10.0.0.5:19999", host="10.0.0.5", port=19999, version="v1.45.3")]

COMPOSE = """
services:
  sonarr:
    image: linuxserver/sonarr
    ports: ["8989:8989"]
    environment:
      SONARR_API_KEY: sup3rsecretkey
"""


@pytest.fixture
def app_d(app, runtime, store):
    """The app with the discovery router mounted (the shared register() does not know about it here).

    conftest's Proxmox double is called `pve`; the installed bot's spec is called `proxmox` and carries the
    same PVE_* settings. Discovery maps products to the real spec names, so the double is given that name
    here — otherwise these tests would pass against a spec that does not exist on a real install.
    """
    from dataclasses import replace

    from periscope_web.routes import discover

    runtime.specs["proxmox"] = replace(runtime.specs["pve"], name="proxmox", title="Proxmox VE")
    store.services["proxmox"] = {"enabled": False, "presence": "default", "env": {"PVE_TOKEN_SECRET": "s3cret"}}
    store.save()
    if not any(getattr(r, "path", "") == "/discover" for r in app.routes):
        app.include_router(discover.router)
    return app


@pytest.fixture
def scanned(app_d):
    """Swap the network scan for a fixed answer, and report which ranges it was asked for."""
    asked: list[str] = []

    async def fake(hosts, *a, **kw):
        asked.append(hosts)
        return list(SCANNED)

    app_d.state.discovery_scan = fake
    return asked


@pytest.fixture
async def dclient(app_d, user):
    """Signed in, and speaking HTMX the way the page itself does."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_d), base_url="http://test",
                                 follow_redirects=False,
                                 headers={"X-CSRF-Token": user.csrf, "HX-Request": "true"},
                                 cookies={SESSION_COOKIE: app_d.state.sessions.cookie_value(user)}) as c:
        yield c


@pytest.fixture
async def danon(app_d):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_d), base_url="http://test",
                                 follow_redirects=False) as c:
        yield c


async def run_scan(client, hosts="10.0.0.0/24"):
    """Start a scan and wait for the background job to finish, the way the page's polling does."""
    r = await client.post("/discover/scan", data={"hosts": hosts, "csrf": "csrf-token-1"})
    assert r.status_code == 200
    for _ in range(50):
        await asyncio.sleep(0.01)
        results = await client.get("/discover/results")
        if "hx-trigger" not in results.text:
            return results
    raise AssertionError("the scan never finished")


# ----- the page ----------------------------------------------------------------------------------------
async def test_the_page_renders_with_a_range_prefilled(dclient):
    r = await dclient.get("/discover")
    assert r.status_code == 200
    assert "Find my services" in r.text and "Scan my network" in r.text
    assert 'name="hosts"' in r.text and "127.0.0.1" in r.text, "the range should arrive prefilled and editable"


async def test_the_page_says_scanning_only_happens_when_you_ask(dclient):
    r = await dclient.get("/discover")
    assert "only ever runs when you press the button" in r.text
    assert "never goes looking on its own" in r.text


async def test_opening_the_page_does_not_start_anything(dclient, scanned):
    await dclient.get("/discover")
    await dclient.get("/discover/results")
    assert scanned == [], "a scan must never start by itself"
    assert "Nothing scanned yet" in (await dclient.get("/discover")).text


async def test_the_page_needs_a_signed_in_admin(danon):
    r = await danon.get("/discover")
    assert r.status_code == 302 and "/login" in r.headers["location"]


async def test_a_scan_cannot_be_started_by_a_stranger(danon):
    r = await danon.post("/discover/scan", data={"hosts": "10.0.0.0/24"})
    assert r.status_code in (302, 401, 403)


async def test_a_scan_cannot_be_started_without_the_csrf_token(app_d, user):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_d), base_url="http://test",
                                 cookies={SESSION_COOKIE: app_d.state.sessions.cookie_value(user)}) as c:
        r = await c.post("/discover/scan", data={"hosts": "10.0.0.0/24"})
    assert r.status_code == 403


# ----- scanning ----------------------------------------------------------------------------------------
async def test_starting_a_scan_answers_at_once_and_polls_for_the_result(dclient, scanned):
    r = await dclient.post("/discover/scan", data={"hosts": "10.0.0.0/24", "csrf": "csrf-token-1"})
    assert r.status_code == 200
    assert "hx-trigger" in r.text and "/discover/results" in r.text, "the first answer should set up polling"
    assert "scanning 10.0.0.0/24" in r.text
    results = await run_scan(dclient)
    assert "hx-trigger" not in results.text, "polling stops once the job is done"


async def test_a_finished_scan_lists_what_it_found(dclient, scanned):
    results = await run_scan(dclient)
    assert scanned == ["10.0.0.0/24"], "the range typed into the box is the one scanned"
    assert "Sonarr" in results.text and "10.0.0.5:8989" in results.text
    assert "Proxmox VE" in results.text and "8.2.2" in results.text
    assert "named from its port" in results.text, "a finding the port named should say so"


async def test_a_product_with_no_service_is_named_but_offers_nothing(dclient, scanned):
    results = await run_scan(dclient)
    assert "Netdata" in results.text
    assert "no service for them" in results.text


async def test_a_scan_that_will_not_run_says_why(dclient, app_d):
    async def boom(hosts, *a, **kw):
        raise ValueError("that is 16777214 addresses — 1024 is the most one scan will take")

    app_d.state.discovery_scan = boom
    results = await run_scan(dclient, hosts="10.0.0.0/8")
    assert "most one scan will take" in results.text
    assert "Fix the range above" in results.text


async def test_a_second_scan_while_one_runs_is_refused(dclient, app_d):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(hosts, *a, **kw):
        started.set()
        await release.wait()
        return []

    app_d.state.discovery_scan = slow
    await dclient.post("/discover/scan", data={"hosts": "10.0.0.0/24", "csrf": "csrf-token-1"})
    await asyncio.wait_for(started.wait(), 1)
    again = await dclient.post("/discover/scan", data={"hosts": "10.0.0.0/24", "csrf": "csrf-token-1"})
    assert "a scan is already running" in again.text
    release.set()


# ----- using a finding ---------------------------------------------------------------------------------
async def test_use_this_fills_the_settings_in_and_runs_the_check(dclient, scanned, reload):
    await run_scan(dclient)
    r = await dclient.post("/discover/use/proxmox", data={"csrf": "csrf-token-1"})
    assert r.status_code == 200
    assert reload().services["proxmox"]["env"]["PVE_URL"] == "https://10.0.0.1:8006"
    assert "checked https://10.0.0.1:8006" in r.text, "the service's own check() should have run"
    assert "Switch it on" in r.text, "a check that passed should offer to switch the service on"


async def test_switching_it_on_after_the_check_passes(dclient, scanned, reload):
    await run_scan(dclient)
    await dclient.post("/discover/use/proxmox", data={"csrf": "csrf-token-1"})
    r = await dclient.post("/discover/enable/proxmox", data={"csrf": "csrf-token-1"})
    assert reload().services["proxmox"]["enabled"] is True
    assert "restart to apply" in r.text


async def test_a_service_with_no_check_is_filled_in_and_says_so(dclient, scanned, reload):
    await run_scan(dclient)
    r = await dclient.post("/discover/use/sonarr", data={"csrf": "csrf-token-1"})
    assert reload().services["sonarr"]["env"]["SONARR_URL"] == "http://10.0.0.5:8989"
    assert "no credentials check" in r.text


async def test_switching_on_is_refused_while_something_required_is_missing(dclient, scanned, reload):
    await run_scan(dclient)
    await dclient.post("/discover/use/sonarr", data={"csrf": "csrf-token-1"})
    r = await dclient.post("/discover/enable/sonarr", data={"csrf": "csrf-token-1"})
    assert reload().services["sonarr"]["enabled"] is False, "an API key is still missing"
    assert "still needs" in r.text


async def test_a_value_already_set_is_never_overwritten(dclient, scanned, store, reload):
    """A URL the user typed themselves must survive a scan that finds the box on another address."""
    store.services["proxmox"]["env"]["PVE_URL"] = "https://pve.lan:8006"
    store.save()
    results = await run_scan(dclient)
    assert "leaves" in results.text and "PVE_URL" in results.text
    r = await dclient.post("/discover/use/proxmox", data={"csrf": "csrf-token-1"})
    assert reload().services["proxmox"]["env"]["PVE_URL"] == "https://pve.lan:8006"
    assert "already has everything" in r.text


async def test_replace_overwrites_only_when_it_is_asked_to(dclient, scanned, store, reload):
    store.services["proxmox"]["env"]["PVE_URL"] = "https://pve.lan:8006"
    store.save()
    await run_scan(dclient)
    await dclient.post("/discover/use/proxmox", data={"csrf": "csrf-token-1", "overwrite": "1"})
    assert reload().services["proxmox"]["env"]["PVE_URL"] == "https://10.0.0.1:8006"


async def test_using_something_that_was_never_found_is_refused(dclient, scanned):
    await run_scan(dclient)
    r = await dclient.post("/discover/use/github", data={"csrf": "csrf-token-1"})
    assert r.status_code == 404 and "scan again" in r.text


# ----- files -------------------------------------------------------------------------------------------
async def test_a_pasted_compose_file_becomes_findings(dclient, reload):
    r = await dclient.post("/discover/import", data={"compose": COMPOSE, "csrf": "csrf-token-1"})
    assert r.status_code == 200
    assert "Sonarr" in r.text and "localhost:8989" in r.text
    assert "from the compose file" in r.text


async def test_a_key_in_a_compose_file_is_marked_but_never_shown(dclient, reload):
    r = await dclient.post("/discover/import", data={"compose": COMPOSE, "csrf": "csrf-token-1"})
    assert "found an API key" in r.text
    assert "sup3rsecretkey" not in r.text, "the key itself must never reach the page"
    assert "1 of them with an API key" in r.text


async def test_a_key_from_a_compose_file_is_still_written_to_the_settings(dclient, reload):
    await dclient.post("/discover/import", data={"compose": COMPOSE, "csrf": "csrf-token-1"})
    r = await dclient.post("/discover/use/sonarr", data={"csrf": "csrf-token-1"})
    env = reload().services["sonarr"]["env"]
    assert env["SONARR_API_KEY"] == "sup3rsecretkey" and env["SONARR_URL"] == "http://localhost:8989"
    assert "sup3rsecretkey" not in r.text


async def test_an_arr_config_folder_is_read(dclient, tmp_path, reload):
    d = tmp_path / "appdata" / "sonarr"
    d.mkdir(parents=True)
    (d / "config.xml").write_text("<Config><Port>8989</Port><ApiKey>xyzkey123</ApiKey><UrlBase></UrlBase>"
                                  "<InstanceName>Sonarr</InstanceName></Config>")
    r = await dclient.post("/discover/import", data={"config_dir": str(tmp_path / "appdata"), "csrf": "csrf-token-1"})
    assert "Sonarr" in r.text and "from its config.xml" in r.text
    assert "xyzkey123" not in r.text
    await dclient.post("/discover/use/sonarr", data={"csrf": "csrf-token-1"})
    assert reload().services["sonarr"]["env"]["SONARR_API_KEY"] == "xyzkey123"


async def test_a_folder_that_is_not_there_says_so(dclient, tmp_path):
    r = await dclient.post("/discover/import", data={"config_dir": str(tmp_path / "nope"), "csrf": "csrf-token-1"})
    assert "is not a folder on this box" in r.text


async def test_compose_that_is_not_yaml_says_so(dclient):
    r = await dclient.post("/discover/import", data={"compose": "services:\n  - [oops\n", "csrf": "csrf-token-1"})
    assert "does not parse as YAML" in r.text


async def test_importing_nothing_asks_for_something(dclient):
    r = await dclient.post("/discover/import", data={"csrf": "csrf-token-1"})
    assert r.status_code == 422
    assert "paste a compose file" in r.text


async def test_a_compose_file_and_a_scan_live_side_by_side(dclient, scanned):
    await dclient.post("/discover/import", data={"compose": COMPOSE, "csrf": "csrf-token-1"})
    results = await run_scan(dclient)
    assert "Proxmox VE" in results.text, "the scan's findings are there"
    assert "found an API key" in results.text, "and the compose import survived the scan"


# ----- the first-run flow ------------------------------------------------------------------------------
async def test_setup_offers_discovery_as_an_optional_step(client):
    r = await client.get("/setup")
    assert r.status_code == 200
    assert "Find what you already run" in r.text
    assert "optional" in r.text and 'href="/discover"' in r.text


async def test_the_optional_step_does_not_count_towards_finishing_setup(client):
    """Setup's four steps are unchanged — discovery is a convenience, not a requirement."""
    r = await client.get("/setup")
    for label in ("Bot token", "Server", "Channel layout", "Add a service"):
        assert label in r.text
    assert r.text.count('class="step ') == 4


async def test_setup_mentions_what_the_scan_would_cover(client):
    r = await client.get("/setup")
    assert "127.0.0.1" in r.text
