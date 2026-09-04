"""What the Engine's payloads mean, and the board rows they turn into. No network anywhere in here."""

from periscope_docker import samples
from periscope_docker.util import (
    block_io,
    chunk_lines,
    container_line,
    counts,
    cpu_percent,
    demux_logs,
    has_update,
    image_tag,
    images_in_use,
    local_digest,
    memory,
    network,
    parse_container,
    parse_containers,
    parse_uptime,
    repo_of,
    sort_key,
    tail,
    watched,
)


def by_name(containers):
    return {c.name: c for c in containers}


def test_container_list_becomes_containers():
    everything = parse_containers(samples.CONTAINERS)
    assert [c.name for c in everything] == ["jellyfin", "traefik", "sonarr", "radarr", "immich-ml", "pgbackup",
                                            "buildx_buildkit_default"]
    c = by_name(everything)
    assert c["jellyfin"].running and c["jellyfin"].health == "healthy" and c["jellyfin"].trouble == ""
    assert c["jellyfin"].short_id == "3f0a1c9d5e7b" and c["jellyfin"].tag == "linuxserver/jellyfin:10.9.11"
    assert c["traefik"].health == "" and c["traefik"].dot == "🟢"            # no health check declared
    assert c["sonarr"].unhealthy and c["sonarr"].trouble == "unhealthy" and c["sonarr"].dot == "🟡"
    assert c["radarr"].exit_code == 137 and c["radarr"].trouble == "crashed" and c["radarr"].dot == "🔴"
    assert c["immich-ml"].state == "restarting" and c["immich-ml"].trouble == "restarting"
    assert c["immich-ml"].dot == "🟡" and c["immich-ml"].exit_code == 1       # the code it keeps dying with
    assert c["pgbackup"].exit_code == 0 and c["pgbackup"].trouble == "stopped" and c["pgbackup"].dot == "⚪"
    assert parse_container({}).name == "" and parse_container({}).trouble == "stopped"


def test_status_lines_give_uptime_health_and_exit_codes():
    assert parse_uptime("Up 12 days (healthy)") == 12 * 86400
    assert parse_uptime("Up About a minute") == 60 and parse_uptime("Up About an hour") == 3600
    assert parse_uptime("Up Less than a second") == 0 and parse_uptime("Up 45 seconds") == 45
    assert parse_uptime("Up 3 weeks") == 3 * 604800 and parse_uptime("Up 2 months") == 2 * 2592000
    assert parse_uptime("Exited (0) 2 hours ago") is None and parse_uptime("Created") is None
    starting = parse_container({"Names": ["/x"], "State": "running", "Status": "Up 4 seconds (health: starting)"})
    assert starting.health == "starting" and not starting.unhealthy and starting.trouble == ""
    assert parse_container({"State": "dead", "Status": "Dead"}).trouble == "dead"
    assert image_tag("nginx@sha256:abcdef0123456789abcdef") == "nginx@sha256:abcdef012345"
    assert image_tag("") == "?" and image_tag("nginx:1.27") == "nginx:1.27"
    assert repo_of("registry.lan:5000/team/app:1.2") == "registry.lan:5000/team/app"
    assert repo_of("nginx") == "nginx" and repo_of("nginx@sha256:aa") == "nginx"


def test_include_then_ignore_decide_what_is_watched():
    names = ["jellyfin", "sonarr", "radarr", "buildx_buildkit_default", "Traefik"]
    assert [n for n in names if watched(n, [], [])] == names                       # empty include = everything
    assert [n for n in names if watched(n, [], ["buildx_*"])] == ["jellyfin", "sonarr", "radarr", "Traefik"]
    assert [n for n in names if watched(n, ["*arr"], [])] == ["sonarr", "radarr"]
    assert [n for n in names if watched(n, ["*arr", "jellyfin"], ["radarr"])] == ["jellyfin", "sonarr"]
    assert watched("Traefik", ["traefik"], []) and watched("traefik", ["TRAEFIK"], [])   # names are not case work
    assert not watched("traefik", ["jelly*"], []) and not watched("", ["*"], ["*"])


def test_board_rows_and_counts():
    rows = sorted(samples.containers(), key=sort_key)
    assert [c.name for c in rows] == ["radarr", "immich-ml", "pgbackup", "sonarr", "jellyfin", "traefik"]
    assert counts(rows) == {"running": 3, "stopped": 2, "unhealthy": 1, "restarting": 1, "total": 6}
    lines = {c.name: container_line(c) for c in rows}
    assert lines["jellyfin"] == "🟢 **jellyfin** `linuxserver/jellyfin:10.9.11` · up 12d · cpu 24% · 1.1 GB"
    assert lines["sonarr"].startswith("🟡 **sonarr** `linuxserver/sonarr:4.0.9` · up 3h · unhealthy · cpu 12%")
    assert lines["radarr"] == "🔴 **radarr** `linuxserver/radarr:5.11.0` · exited (137)"
    assert lines["pgbackup"].endswith("· stopped") and "restarting · last exit 1" in lines["immich-ml"]
    assert "immich-machine…" in lines["immich-ml"]                                  # long tags are cut, not wrapped
    # rows are grouped into blocks a single embed field can hold
    blocks = chunk_lines([f"line {i} " + "x" * 90 for i in range(30)])
    assert len(blocks) == 3 and all(len(b) <= 1000 for b in blocks)
    assert chunk_lines([]) == [] and chunk_lines(["one"]) == ["one"]
    assert images_in_use(rows) == ["ghcr.io/immich-app/immich-machine-learning:v1.117.0",
                                   "linuxserver/jellyfin:10.9.11", "linuxserver/radarr:5.11.0",
                                   "linuxserver/sonarr:4.0.9", "prodrigestivill/postgres-backup-local:16",
                                   "traefik:v3.1"]


def test_stats_sample_gives_the_numbers_docker_stats_shows():
    sample = samples.STATS["jellyfin"]
    assert cpu_percent(sample) == 24.0                       # 0.3s of cpu in 10s of machine time, across 8 cpus
    used, limit = memory(sample)
    assert (used, limit) == (1_207_959_552, 8_589_934_592)   # page cache does not count as used memory
    assert network(sample) == (9_876_543_210, 1_234_567_890)
    assert block_io(sample) == (5_368_709_120, 1_073_741_824)
    # a first sample has nothing to compare against, and a container with no limit reports none
    assert cpu_percent({"cpu_stats": {}, "precpu_stats": {}}) is None and cpu_percent(None) is None
    assert memory({"memory_stats": {"usage": 1024, "limit": 0}}) == (1024, None)
    assert memory({}) == (None, None) and network({}) == (0, 0) and block_io({}) == (0, 0)


def test_log_stream_is_unframed_before_it_is_shown():
    assert demux_logs(samples.log_stream()) == "\n".join(samples.LOG_LINES) + "\n"
    assert demux_logs(samples.log_stream(["only stderr"], stream=2)) == "only stderr\n"
    assert demux_logs(b"a container with a tty writes plain bytes\n") == "a container with a tty writes plain bytes\n"
    assert demux_logs(b"") == ""
    text = "\n".join(f"line {i}" for i in range(100))
    assert tail(text, 3) == "line 97\nline 98\nline 99"
    assert tail("a\n\n  \nb", 5) == "a\nb" and tail("", 5) == ""
    assert len(tail(text, 100, limit=20)) == 20 and tail(text, 100, limit=20).startswith("…")


def test_an_image_is_out_of_date_when_the_registry_moved_on():
    by_tag = {img["RepoTags"][0]: img for img in samples.IMAGES}
    sonarr = by_tag["linuxserver/sonarr:4.0.9"]
    assert local_digest(sonarr).startswith("sha256:cccc")
    assert has_update(sonarr, samples.REGISTRY_DIGESTS["linuxserver/sonarr:4.0.9"])
    jellyfin = by_tag["linuxserver/jellyfin:10.9.11"]
    assert not has_update(jellyfin, samples.REGISTRY_DIGESTS["linuxserver/jellyfin:10.9.11"])
    assert not has_update(jellyfin, "")                       # the registry would not say: never an update
    assert not has_update(by_tag["<none>:<none>"], "sha256:whatever")   # built here, never pushed
    # several repos on one image: the digest of the repo asked about is the one compared
    shared = {"RepoTags": ["nginx:1.27", "registry.lan:5000/nginx:1.27"],
              "RepoDigests": ["registry.lan:5000/nginx@sha256:local", "nginx@sha256:hub"]}
    assert local_digest(shared, "nginx:1.27") == "sha256:hub"
    assert local_digest(shared, "registry.lan:5000/nginx:1.27") == "sha256:local"
