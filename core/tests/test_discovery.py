"""Discovery: identifying a product from its answer, the scan's bounds, and reading compose/config files.

No socket is opened anywhere in here. `probe_targets()` takes a connector and `identify()` takes a fetcher,
so both are handed fakes; the fixture bodies below are written from each product's published API shape (see
FIXTURES) rather than captured from a live host.
"""

from __future__ import annotations

import asyncio

import pytest

from periscope.discovery import (
    DEFAULT_PORTS,
    PORT_HINTS,
    Found,
    Reply,
    default_hosts,
    expand_hosts,
    from_arr_config,
    from_compose,
    identify,
    probe_targets,
    scan,
    suggestions,
)

# ----- recorded-shape responses, one per product -------------------------------------------------------
# path -> Reply. Keyed by the exact path each probe asks for, so a probe that asks the wrong thing gets a 404
# and fails the same way it would in real life.
ARR_401 = Reply(401, '{"error":"Unauthorized"}', {"Content-Type": "application/json"})

FIXTURES: dict[str, dict[str, Reply]] = {
    # *arr: no key -> 401 and a short JSON body. None of them name themselves here; the port does that.
    "sonarr": {"/api/v3/system/status": ARR_401},
    "radarr": {"/api/v3/system/status": ARR_401},
    "lidarr": {"/api/v1/system/status": ARR_401},
    "prowlarr": {"/api/v1/system/status": ARR_401},
    "qbittorrent": {"/api/v2/app/version": Reply(200, "v4.6.5", {"Content-Type": "text/plain"})},
    "sabnzbd": {"/api?mode=version&output=json": Reply(200, '{"version": "4.2.3"}')},
    "plex": {"/identity": Reply(200, '<?xml version="1.0" encoding="UTF-8"?>\n'
                                     '<MediaContainer size="0" claimed="1" machineIdentifier="a1b2c3d4e5" '
                                     'version="1.40.1.8227-c0dd5a73e">\n</MediaContainer>',
                                {"Content-Type": "application/xml"})},
    "jellyfin": {"/System/Info/Public": Reply(200, '{"LocalAddress":"http://10.0.0.9:8096","ServerName":"attic",'
                                                   '"Version":"10.9.7","ProductName":"Jellyfin Server",'
                                                   '"Id":"6f3c0b1e"}')},
    "overseerr": {"/api/v1/status": Reply(200, '{"version":"1.33.2","commitTag":"v1.33.2",'
                                               '"updateAvailable":false,"commitsBehind":0}')},
    "prometheus": {"/-/ready": Reply(200, "Prometheus Server is Ready.\n", {"Content-Type": "text/plain"})},
    "alertmanager": {"/api/v2/status": Reply(200, '{"cluster":{"status":"ready"},"versionInfo":'
                                                  '{"version":"0.27.0","branch":"HEAD"},"uptime":"2024-05-01"}')},
    "grafana": {"/api/health": Reply(200, '{"commit":"cef9c4c","database":"ok","version":"11.1.0"}')},
    "proxmox": {"/api2/json/version": Reply(200, '{"data":{"version":"8.2.2","release":"8.2",'
                                                 '"repoid":"9355359cd7afbae4"}}')},
    "unifi": {"/status": Reply(200, '{"meta":{"rc":"ok","server_version":"8.1.113","up":true},"data":[]}')},
    "docker": {"/version": Reply(200, '{"Platform":{"Name":"Docker Engine - Community"},"Version":"26.1.3",'
                                      '"ApiVersion":"1.45","Os":"linux","Arch":"amd64"}')},
    "portainer": {"/api/status": Reply(200, '{"Version":"2.20.3","InstanceID":"b0a7f1c2-3d4e","DatabaseVersion":80}')},
    "netdata": {"/api/v1/info": Reply(200, '{"version":"v1.45.3","uid":"7c2f1a90","mirrored_hosts":["attic"],'
                                           '"os_name":"Debian GNU/Linux"}')},
}

# the port each product is normally reached on, for the identify() round-trip
HOME_PORT = {"sonarr": 8989, "radarr": 7878, "lidarr": 8686, "prowlarr": 9696, "qbittorrent": 8080,
             "sabnzbd": 8081, "plex": 32400, "jellyfin": 8096, "overseerr": 5055, "prometheus": 9090,
             "alertmanager": 9093, "grafana": 3000, "proxmox": 8006, "unifi": 8443, "docker": 2375,
             "portainer": 9000, "netdata": 19999}


def fake_fetch(pages: dict[str, Reply], *, seen: list[str] | None = None):
    """A fetcher over one product's recorded answers: anything it was not asked about 404s."""

    async def fetch(url: str) -> Reply:
        path = url.split("://", 1)[-1]
        path = path[path.index("/"):] if "/" in path else "/"
        if seen is not None:
            seen.append(path)
        return pages.get(path, Reply(404, "not found"))

    return fetch


# ----- identify ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("product", sorted(FIXTURES))
async def test_identify_names_each_product(product):
    port = HOME_PORT[product]
    got = await identify(f"http://box:{port}", fetch=fake_fetch(FIXTURES[product]))
    assert got is not None, f"{product} was not identified"
    assert got.service == product
    assert got.host == "box" and got.port == port
    assert got.source == "scan"


@pytest.mark.parametrize("product,version", [("qbittorrent", "4.6.5"), ("sabnzbd", "4.2.3"),
                                             ("plex", "1.40.1.8227-c0dd5a73e"), ("jellyfin", "10.9.7"),
                                             ("overseerr", "1.33.2"), ("alertmanager", "0.27.0"),
                                             ("grafana", "11.1.0"), ("proxmox", "8.2.2"), ("unifi", "8.1.113"),
                                             ("docker", "26.1.3"), ("portainer", "2.20.3"),
                                             ("netdata", "v1.45.3")])
async def test_identify_keeps_the_version_the_product_volunteered(product, version):
    got = await identify(f"http://box:{HOME_PORT[product]}", fetch=fake_fetch(FIXTURES[product]))
    assert got is not None and got.version == version


async def test_identify_asks_the_port_s_product_first():
    seen: list[str] = []
    await identify("http://box:3000", fetch=fake_fetch(FIXTURES["grafana"], seen=seen))
    assert seen == ["/api/health"], "the port's own candidate should be the only request needed"


async def test_identify_finds_a_product_on_an_unexpected_port():
    """A Grafana on 8099 still gets found — it just costs the other candidates first."""
    got = await identify("http://box:8099", fetch=fake_fetch(FIXTURES["grafana"]))
    assert got is not None and got.service == "grafana" and got.port == 8099


async def test_identify_tries_the_other_scheme_when_nothing_answers():
    """A port that connects but answers nothing is usually the other scheme — a Proxmox behind plain http."""
    tried: list[str] = []

    async def fetch(url: str) -> Reply:
        tried.append(url)
        if url.startswith("https://"):
            raise OSError("ssl handshake failed")
        path = url[url.index("/", 8):]
        return FIXTURES["proxmox"].get(path, Reply(404))

    got = await identify("https://box:8006", fetch=fetch)
    assert got is not None and got.service == "proxmox" and got.url == "http://box:8006"
    assert any(u.startswith("https://") for u in tried) and any(u.startswith("http://") for u in tried)


async def test_the_scheme_is_only_flipped_once():
    tried: list[str] = []

    async def fetch(url: str) -> Reply:
        tried.append(url)
        raise OSError("nothing there")

    assert await identify("https://box:8006", fetch=fetch, candidates=["proxmox"]) is None
    assert tried == ["https://box:8006/api2/json/version", "http://box:8006/api2/json/version"]


async def test_a_port_that_answers_but_is_not_ours_is_not_retried():
    """A 404 is an answer, so there is no reason to try the other scheme."""
    tried: list[str] = []

    async def fetch(url: str) -> Reply:
        tried.append(url)
        return Reply(404, "nope")

    assert await identify("https://box:8006", fetch=fetch, candidates=["proxmox"]) is None
    assert tried == ["https://box:8006/api2/json/version"]


async def test_identify_returns_none_when_nothing_recognisable_answers():
    pages = {"/": Reply(200, "<html><body>hello</body></html>")}
    assert await identify("http://box:8080", fetch=fake_fetch(pages)) is None


async def test_identify_survives_a_candidate_that_raises():
    async def fetch(url: str) -> Reply:
        if "system/status" in url:
            raise OSError("connection reset")
        return FIXTURES["grafana"].get("/api/health", Reply(404)) if url.endswith("/api/health") else Reply(404)

    got = await identify("http://box:8989", fetch=fetch)
    assert got is not None and got.service == "grafana"


async def test_arr_401_names_the_arr_from_the_port_and_says_so():
    """The 401 proves it is an *arr but not which one, so the finding is marked `family`, not `named`."""
    got = await identify("http://box:7878", fetch=fake_fetch(FIXTURES["radarr"]))
    assert got is not None and got.service == "radarr"
    assert got.confidence == "family"
    assert "401" in got.note


async def test_an_arr_that_names_itself_is_named_not_guessed():
    pages = {"/api/v3/system/status": Reply(401, '{"error":"Unauthorized"}',
                                            {"X-Application-Name": "Sonarr", "X-Application-Version": "4.0.9"})}
    got = await identify("http://box:9999", fetch=fake_fetch(pages), candidates=["sonarr"])
    assert got is not None and got.service == "sonarr"
    assert got.confidence == "named" and got.version == "4.0.9"


async def test_a_200_on_an_arr_api_is_not_an_arr():
    pages = {"/api/v3/system/status": Reply(200, '{"appName":"Sonarr"}')}
    assert await identify("http://box:8989", fetch=fake_fetch(pages), candidates=["sonarr"]) is None


async def test_products_are_not_confused_with_each_other():
    """Every fixture is offered to every other product's probe; only its own may match."""
    for product, pages in FIXTURES.items():
        for other in FIXTURES:
            if other == product or (product in ("sonarr", "radarr", "lidarr", "prowlarr")
                                    and other in ("sonarr", "radarr", "lidarr", "prowlarr")):
                continue    # the *arrs genuinely share one response; the port tells them apart
            got = await identify("http://box:1", fetch=fake_fetch(pages), candidates=[other])
            assert got is None, f"{other}'s probe matched {product}'s response"


# ----- probe_targets -----------------------------------------------------------------------------------
class Overlap:
    """Counts how many connections were in the air at the same time."""

    def __init__(self):
        self.now = 0
        self.peak = 0

    def enter(self):
        self.now += 1
        self.peak = max(self.peak, self.now)

    def leave(self):
        self.now -= 1


def fake_connector(open_pairs: set[tuple[str, int]], *, log: list | None = None,
                   overlap: Overlap | None = None, delay: float = 0.0):
    """A connector that says yes only to `open_pairs`, and records how many calls overlapped."""

    async def connect(host: str, port: int, timeout: float) -> bool:
        if log is not None:
            log.append((host, port, timeout))
        if overlap is not None:
            overlap.enter()
        try:
            if delay:
                await asyncio.sleep(delay)
        finally:
            if overlap is not None:
                overlap.leave()
        return (host, port) in open_pairs

    return connect


async def test_probe_targets_reports_only_what_answered():
    calls: list = []
    connect = fake_connector({("10.0.0.5", 8989), ("10.0.0.7", 3000)}, log=calls)
    got = await probe_targets(["10.0.0.5", "10.0.0.7"], [8989, 3000], connect=connect)
    assert [(t.host, t.port) for t in got] == [("10.0.0.5", 8989), ("10.0.0.7", 3000)]
    assert len(calls) == 4, "every host/port pair should be tried exactly once"


async def test_probe_targets_never_exceeds_its_in_flight_bound():
    overlap = Overlap()
    connect = fake_connector(set(), overlap=overlap, delay=0.002)
    await probe_targets("10.0.0.0/24", [8989, 3000], in_flight=8, connect=connect)
    assert overlap.peak <= 8, f"{overlap.peak} connections were in flight at once, the bound was 8"
    assert overlap.peak > 1, "a bounded scan should still overlap, or a /24 would take all day"


async def test_probe_targets_passes_its_timeout_down():
    calls: list = []
    await probe_targets(["10.0.0.5"], [8989], timeout=0.25, connect=fake_connector(set(), log=calls))
    assert calls == [("10.0.0.5", 8989, 0.25)]


async def test_a_connector_that_times_out_is_just_a_closed_port():
    async def connect(host, port, timeout):
        raise asyncio.TimeoutError

    assert await probe_targets(["10.0.0.5"], [8989], connect=connect) == []


async def test_a_connector_that_raises_does_not_end_the_scan():
    async def connect(host, port, timeout):
        if host == "10.0.0.5":
            raise RuntimeError("boom")
        return port == 3000

    got = await probe_targets(["10.0.0.5", "10.0.0.6"], [3000], connect=connect)
    assert [(t.host, t.port) for t in got] == [("10.0.0.6", 3000)]


async def test_probe_targets_defaults_to_the_usual_ports():
    calls: list = []
    await probe_targets(["10.0.0.5"], connect=fake_connector(set(), log=calls))
    assert {p for _, p, _ in calls} == set(DEFAULT_PORTS)
    assert 8989 in DEFAULT_PORTS and 32400 in DEFAULT_PORTS


def test_a_target_knows_whether_its_port_speaks_tls():
    from periscope.discovery import Target
    assert Target("10.0.0.5", 8989).url == "http://10.0.0.5:8989"
    assert Target("10.0.0.5", 8006).url == "https://10.0.0.5:8006"


# ----- host ranges -------------------------------------------------------------------------------------
def test_expand_hosts_accepts_a_cidr_a_list_and_a_typed_string():
    assert len(expand_hosts("192.168.1.0/24")) == 254
    assert expand_hosts(["10.0.0.1", "10.0.0.2"]) == ["10.0.0.1", "10.0.0.2"]
    assert expand_hosts("10.0.0.1, 10.0.0.2  10.0.0.1") == ["10.0.0.1", "10.0.0.2"]


def test_expand_hosts_passes_names_through():
    assert expand_hosts("sonarr.lan") == ["sonarr.lan"]


def test_expand_hosts_refuses_a_range_too_big_to_be_polite():
    with pytest.raises(ValueError, match="most one scan will take"):
        expand_hosts("10.0.0.0/8")


def test_expand_hosts_rejects_nonsense():
    with pytest.raises(ValueError, match="not a network"):
        expand_hosts("192.168.1.0/nope")


def test_default_hosts_always_includes_localhost():
    hosts = default_hosts()
    assert hosts[0] == "127.0.0.1"
    assert all(isinstance(h, str) and h for h in hosts)


# ----- scan end to end (still no sockets) ---------------------------------------------------------------
async def test_scan_probes_then_identifies():
    connect = fake_connector({("10.0.0.5", 3000)})
    got = await scan(["10.0.0.5", "10.0.0.6"], [3000], connect=connect, fetch=fake_fetch(FIXTURES["grafana"]))
    assert [(f.service, f.host) for f in got] == [("grafana", "10.0.0.5")]


async def test_scan_identifies_several_boxes_in_one_run():
    """Two open ports on two hosts, each a different product, all through one fetcher."""
    async def fetch(url: str) -> Reply:
        host = url.split("://", 1)[1].split(":")[0]
        pages = FIXTURES["grafana"] if host == "10.0.0.5" else FIXTURES["proxmox"]
        path = url[url.index("/", url.index("://") + 3):]
        return pages.get(path, Reply(404))

    connect = fake_connector({("10.0.0.5", 3000), ("10.0.0.9", 8006)})
    got = await scan(["10.0.0.5", "10.0.0.9"], [3000, 8006], connect=connect, fetch=fetch)
    assert [(f.service, f.host) for f in got] == [("grafana", "10.0.0.5"), ("proxmox", "10.0.0.9")]


async def test_scan_of_a_silent_range_finds_nothing_and_makes_no_requests():
    asked: list[str] = []

    async def fetch(url):
        asked.append(url)
        return Reply(404)

    assert await scan("10.0.0.0/28", [3000], connect=fake_connector(set()), fetch=fetch) == []
    assert asked == []


# ----- compose -----------------------------------------------------------------------------------------
COMPOSE = """
services:
  sonarr:
    image: lscr.io/linuxserver/sonarr:latest
    ports:
      - "8989:8989"
    environment:
      - PUID=1000
      - SONARR_API_KEY=abc123def456
  radarr:
    image: linuxserver/radarr
    ports: ["127.0.0.1:7878:7878/tcp"]
    environment:
      API_KEY: rrrkey789
  prowlarr:
    image: ghcr.io/linuxserver/prowlarr:develop
    ports:
      - target: 9696
        published: 9696
  plex:
    image: plexinc/pms-docker:latest
    network_mode: host
  grafana:
    image: grafana/grafana-oss:11.1.0
    ports:
      - "3000:3000"
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASS}
  db:
    image: postgres:16
    ports:
      - "5432:5432"
"""


def test_from_compose_names_services_from_their_images():
    found = {f.service: f for f in from_compose(COMPOSE)}
    assert set(found) == {"sonarr", "radarr", "prowlarr", "plex", "grafana"}, "postgres is not ours to claim"
    assert found["sonarr"].url == "http://localhost:8989"
    assert found["radarr"].url == "http://localhost:7878", "the host side of the mapping is the one to use"
    assert found["prowlarr"].port == 9696, "long-form ports work too"
    assert all(f.source == "compose" for f in found.values())


def test_from_compose_falls_back_to_the_usual_port_when_none_is_published():
    plex = next(f for f in from_compose(COMPOSE) if f.service == "plex")
    assert plex.port == 32400 and plex.url == "http://localhost:32400"


def test_from_compose_picks_up_an_api_key_without_putting_it_on_show():
    sonarr = next(f for f in from_compose(COMPOSE) if f.service == "sonarr")
    assert sonarr.settings["SONARR_API_KEY"] == "abc123def456"
    assert sonarr.secret_keys == ("SONARR_API_KEY",) and sonarr.has_secret
    assert "abc123def456" not in repr(sonarr), "a key must never reach a log line"
    assert sonarr.redacted().settings["SONARR_API_KEY"] == ""
    assert "found" in repr(sonarr) or "key for" in repr(sonarr)


def test_from_compose_reads_a_key_from_a_mapping_style_environment():
    radarr = next(f for f in from_compose(COMPOSE) if f.service == "radarr")
    assert radarr.settings["RADARR_API_KEY"] == "rrrkey789"


def test_from_compose_ignores_an_unresolved_variable():
    grafana = next(f for f in from_compose(COMPOSE) if f.service == "grafana")
    assert not grafana.settings and not grafana.has_secret


def test_from_compose_does_not_mistake_prowlarr_for_radarr():
    assert {f.service for f in from_compose("services:\n  x:\n    image: linuxserver/prowlarr\n")} == {"prowlarr"}


def test_from_compose_reads_a_file(tmp_path):
    p = tmp_path / "docker-compose.yml"
    p.write_text(COMPOSE)
    assert {f.service for f in from_compose(p)} == {f.service for f in from_compose(COMPOSE)}
    assert {f.service for f in from_compose(str(p))} == {f.service for f in from_compose(COMPOSE)}


def test_from_compose_can_point_the_urls_at_another_box():
    sonarr = next(f for f in from_compose(COMPOSE, host="10.0.0.9") if f.service == "sonarr")
    assert sonarr.url == "http://10.0.0.9:8989"


def test_from_compose_says_so_when_the_text_is_not_yaml():
    with pytest.raises(ValueError, match="does not parse as YAML"):
        from_compose("services:\n  - [unclosed\n")


def test_from_compose_of_something_that_is_not_compose_is_empty():
    assert from_compose("just: a mapping\n") == []
    assert from_compose("- a\n- list\n") == []


# ----- *arr config.xml ---------------------------------------------------------------------------------
CONFIG_XML = """<?xml version="1.0" encoding="utf-8"?>
<Config>
  <BindAddress>*</BindAddress>
  <Port>8989</Port>
  <SslPort>9898</SslPort>
  <EnableSsl>False</EnableSsl>
  <LaunchBrowser>True</LaunchBrowser>
  <ApiKey>4f9a1c2b3d4e5f60718293a4b5c6d7e8</ApiKey>
  <AuthenticationMethod>Forms</AuthenticationMethod>
  <Branch>main</Branch>
  <UrlBase></UrlBase>
  <InstanceName>Sonarr</InstanceName>
</Config>
"""


def test_from_arr_config_reads_port_and_key(tmp_path):
    p = tmp_path / "sonarr" / "config.xml"
    p.parent.mkdir()
    p.write_text(CONFIG_XML)
    (found,) = from_arr_config(p)
    assert found.service == "sonarr" and found.port == 8989
    assert found.url == "http://localhost:8989"
    assert found.settings["SONARR_API_KEY"] == "4f9a1c2b3d4e5f60718293a4b5c6d7e8"
    assert found.source == "config" and found.secret_keys == ("SONARR_API_KEY",)
    assert "4f9a1c2b" not in repr(found)


def test_from_arr_config_uses_the_url_base(tmp_path):
    p = tmp_path / "radarr" / "config.xml"
    p.parent.mkdir()
    p.write_text(CONFIG_XML.replace("<UrlBase></UrlBase>", "<UrlBase>/movies</UrlBase>")
                           .replace("<Port>8989</Port>", "<Port>7878</Port>")
                           .replace("<InstanceName>Sonarr</InstanceName>", "<InstanceName>Radarr</InstanceName>"))
    (found,) = from_arr_config(p)
    assert found.service == "radarr" and found.url == "http://localhost:7878/movies"


def test_from_arr_config_prefers_the_port_in_the_file_over_the_usual_one(tmp_path):
    """A Sonarr moved to 8123 is reported on 8123, not on the port Sonarr usually takes."""
    p = tmp_path / "sonarr" / "config.xml"
    p.parent.mkdir()
    p.write_text(CONFIG_XML.replace("<Port>8989</Port>", "<Port>8123</Port>"))
    (found,) = from_arr_config(p)
    assert found.port == 8123 and found.url == "http://localhost:8123"


def test_from_arr_config_follows_enable_ssl_to_the_ssl_port(tmp_path):
    p = tmp_path / "sonarr" / "config.xml"
    p.parent.mkdir()
    p.write_text(CONFIG_XML.replace("<EnableSsl>False</EnableSsl>", "<EnableSsl>True</EnableSsl>"))
    (found,) = from_arr_config(p)
    assert found.url == "https://localhost:9898" and found.port == 9898


def test_from_arr_config_names_the_app_from_its_folder_when_there_is_no_instance_name(tmp_path):
    p = tmp_path / "lidarr" / "config.xml"
    p.parent.mkdir()
    p.write_text(CONFIG_XML.replace("<InstanceName>Sonarr</InstanceName>", ""))
    (found,) = from_arr_config(p)
    assert found.service == "lidarr"


def test_from_arr_config_accepts_the_directory(tmp_path):
    d = tmp_path / "prowlarr"
    d.mkdir()
    (d / "config.xml").write_text(CONFIG_XML.replace("Sonarr", "Prowlarr"))
    (found,) = from_arr_config(d)
    assert found.service == "prowlarr"


def test_from_arr_config_of_an_unrecognisable_file_is_empty(tmp_path):
    p = tmp_path / "somewhere" / "config.xml"
    p.parent.mkdir()
    p.write_text(CONFIG_XML.replace("<InstanceName>Sonarr</InstanceName>", ""))
    assert from_arr_config(p) == []
    q = tmp_path / "other.xml"
    q.write_text("<NotConfig><Port>1</Port></NotConfig>")
    assert from_arr_config(q) == []


def test_from_arr_config_says_so_when_the_file_will_not_read(tmp_path):
    p = tmp_path / "sonarr" / "config.xml"
    p.parent.mkdir()
    p.write_text("<Config><Port>8989</Port>")
    with pytest.raises(ValueError, match="could not read"):
        from_arr_config(p)
    with pytest.raises(ValueError):
        from_arr_config(tmp_path / "sonarr" / "missing.xml")


# ----- suggestions -------------------------------------------------------------------------------------
class FakeStore:
    """Just the two things suggestions() asks a Store for."""

    def __init__(self, env=None, services=None):
        self._env = env or {}
        self.services = services or {}

    def env_for(self, name):
        return dict(self._env.get(name, {}))


def test_suggestions_use_the_setting_keys_the_specs_declare():
    found = [Found("sonarr", url="http://10.0.0.5:8989"), Found("proxmox", url="https://10.0.0.1:8006"),
             Found("overseerr", url="http://10.0.0.5:5055")]
    by_name = {s.service: s for s in suggestions(found, FakeStore())}
    assert by_name["sonarr"].settings == {"SONARR_URL": "http://10.0.0.5:8989"}
    assert by_name["proxmox"].settings == {"PVE_URL": "https://10.0.0.1:8006"}
    assert by_name["plexrequests"].settings == {"OVERSEERR_URL": "http://10.0.0.5:5055"}, "overseerr → plexrequests"


def test_suggestions_never_overwrite_a_value_the_user_already_set():
    store = FakeStore(env={"sonarr": {"SONARR_URL": "http://mine:8989"}})
    (s,) = suggestions([Found("sonarr", url="http://10.0.0.5:8989")], store)
    assert s.settings == {}, "a URL already in the store must survive a scan"
    assert s.skipped["SONARR_URL"] == "already set — left as it is"
    assert s.already_configured and s.writes_nothing


def test_suggestions_overwrite_only_when_they_are_asked_to():
    store = FakeStore(env={"sonarr": {"SONARR_URL": "http://mine:8989"}})
    (s,) = suggestions([Found("sonarr", url="http://10.0.0.5:8989")], store, overwrite=True)
    assert s.settings == {"SONARR_URL": "http://10.0.0.5:8989"}


def test_suggestions_fill_only_the_empty_keys():
    """URL already set, API key not: write the key, leave the URL alone."""
    store = FakeStore(env={"sonarr": {"SONARR_URL": "http://mine:8989", "SONARR_API_KEY": ""}})
    found = Found("sonarr", url="http://10.0.0.5:8989", settings={"SONARR_API_KEY": "k"},
                  secret_keys=("SONARR_API_KEY",), source="config")
    (s,) = suggestions([found], store)
    assert s.settings == {"SONARR_API_KEY": "k"}
    assert "SONARR_URL" in s.skipped and s.has_secret


def test_a_suggestion_never_shows_the_key_it_would_write():
    found = Found("sonarr", url="http://x:8989", settings={"SONARR_API_KEY": "sup3rsecret"},
                  secret_keys=("SONARR_API_KEY",))
    (s,) = suggestions([found], FakeStore())
    assert s.settings["SONARR_API_KEY"] == "sup3rsecret", "it still has to be written"
    assert "sup3rsecret" not in repr(s)
    assert s.redacted().settings["SONARR_API_KEY"] == ""
    assert s.redacted().found.settings["SONARR_API_KEY"] == ""


def test_suggestions_report_whether_the_service_is_already_on():
    store = FakeStore(env={"sonarr": {}}, services={"sonarr": {"enabled": True}})
    (s,) = suggestions([Found("sonarr", url="http://x:8989")], store)
    assert s.enabled and not s.already_configured


def test_suggestions_drop_products_periscope_has_no_service_for():
    assert suggestions([Found("netdata", url="http://x:19999")], FakeStore()) == []


def test_suggestions_keep_one_per_service_preferring_the_source_with_the_key():
    scanned = Found("sonarr", url="http://10.0.0.5:8989", source="scan", confidence="family")
    from_file = Found("sonarr", url="http://localhost:8989", source="config",
                      settings={"SONARR_API_KEY": "k"}, secret_keys=("SONARR_API_KEY",))
    (s,) = suggestions([scanned, from_file], FakeStore())
    assert s.settings == {"SONARR_URL": "http://localhost:8989", "SONARR_API_KEY": "k"}
    assert s.found.source == "config"


def test_suggestions_survive_a_store_that_cannot_answer():
    class Broken:
        services = {}

        def env_for(self, name):
            raise RuntimeError("no config yet")

    (s,) = suggestions([Found("grafana", url="http://x:3000")], Broken())
    assert s.settings == {"GRAFANA_URL": "http://x:3000"}


def test_suggestions_of_nothing_is_nothing():
    assert suggestions([], FakeStore()) == []


# ----- the port table itself ---------------------------------------------------------------------------
def test_every_hinted_product_has_a_probe():
    from periscope.discovery import PROBES
    for port, names in PORT_HINTS.items():
        for name in names:
            assert name in PROBES, f"port {port} points at {name}, which has no probe"


def test_every_probe_product_is_reachable_from_some_port():
    from periscope.discovery import PROBES
    hinted = {n for names in PORT_HINTS.values() for n in names}
    assert set(PROBES) == hinted, "a product with a probe but no port is never asked about first"


def test_the_ports_the_brief_asked_for_are_all_covered():
    expected = {8989: "sonarr", 7878: "radarr", 8686: "lidarr", 9696: "prowlarr", 32400: "plex",
                8096: "jellyfin", 5055: "overseerr", 9090: "prometheus", 9093: "alertmanager",
                3000: "grafana", 8006: "proxmox", 19999: "netdata"}
    for port, name in expected.items():
        assert PORT_HINTS[port][0] == name
    assert set(PORT_HINTS[8080]) == set(PORT_HINTS[8081]) == {"qbittorrent", "sabnzbd"}
    assert PORT_HINTS[443] == PORT_HINTS[8443] == ("unifi",)
    assert PORT_HINTS[2375] == PORT_HINTS[2376] == ("docker",)
