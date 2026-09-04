"""What a run of polls fires and resolves: crashes, clean stops, health checks, restart loops, a dead daemon."""

import time
from types import SimpleNamespace

import pytest
from periscope import JsonState, Severity, env_scope
from periscope.messages import Messages

from periscope_docker import samples
from periscope_docker.cogs.status import (
    FP_UNREACHABLE,
    FP_UPDATES,
    MAX_FAILURES,
    RESTART_WINDOW_S,
    StatusCog,
    restarted,
)
from periscope_docker.config import DockerConfig
from periscope_docker.util import parse_containers


def container(name, state="running", status="Up 2 days", image="ghcr.io/lab/app:1.0"):
    return {"Id": (name * 12)[:64], "Names": [f"/{name}"], "Image": image, "State": state, "Status": status,
            "Created": 1725000000, "Labels": {}}


UP = [container("jellyfin", status="Up 12 days (healthy)"), container("traefik")]


class FakeAlerts:
    """Records what the router would have done, and knows which fingerprints are open."""

    def __init__(self):
        self.fired, self.resolved, self.open = [], [], {}

    async def fire(self, alert, force=False):
        self.fired.append(alert)
        self.open[alert.fingerprint] = alert
        return SimpleNamespace(id=len(self.fired))

    async def resolve(self, fingerprint, note=None):
        self.resolved.append((fingerprint, note))
        return self.open.pop(fingerprint, None) is not None

    def active(self):
        return list(self.open)

    def titles(self):
        return [a.title for a in self.fired]


class FakeDocker:
    def __init__(self):
        self.polls: list[list[dict]] = [UP]
        self.error: Exception | None = None
        self.cached = []
        self.update_rows: list[dict] = []
        self.sampled = 0

    async def containers(self, all_containers=True):
        if self.error is not None:
            raise self.error
        payload = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        self.cached = parse_containers(payload)
        return self.cached

    async def version(self):
        return samples.VERSION

    async def sample(self, containers, limit=12):
        self.sampled += 1

    async def updates(self, refs):
        return list(self.update_rows)


class FakeBot:
    def __init__(self, tmp_path, cfg):
        self.cfg = cfg
        self.name = "docker"
        self.lab_name = "THE LAB"
        self.state = JsonState(tmp_path / "state.json")
        self.messages = Messages(None, service="docker", lab="THE LAB")
        self.alerts = FakeAlerts()
        self.docker = FakeDocker()
        # no status channel: the poll evaluates alerts and never touches Discord
        self.settings = SimpleNamespace(status_channel_id=None, alert_channel_id=2, status_interval_s=60)
        self.views = []

    def add_view(self, view):
        self.views.append(view)

    async def get_channel_safe(self, cid):
        return None


def make(tmp_path, **env) -> StatusCog:
    with env_scope({str(k): str(v) for k, v in env.items()}):
        cfg = DockerConfig.from_env()
    return StatusCog(FakeBot(tmp_path, cfg))


async def poll(cog, payload=None):
    if payload is not None:
        cog.bot.docker.polls = [payload]
    await cog.tick.coro(cog)


@pytest.mark.asyncio
async def test_a_crash_alerts_and_a_restart_resolves_it(tmp_path):
    cog = make(tmp_path)
    alerts = cog.bot.alerts
    await poll(cog, UP)
    assert alerts.fired == [] and cog._version == "27.1.1" and cog.bot.docker.sampled == 1

    await poll(cog, UP + [container("radarr", "exited", "Exited (137) 6 minutes ago")])
    (alert,) = alerts.fired
    assert alert.fingerprint == "docker:container:radarr:exited" and alert.severity is Severity.CRITICAL
    assert alert.title == "Container crashed: radarr" and "exit code **137**" in alert.description
    assert alert.fields["Image"] == "`ghcr.io/lab/app:1.0`"

    await poll(cog, UP + [container("radarr")])
    assert alerts.resolved == [("docker:container:radarr:exited", "`radarr` is running again")]
    assert alerts.active() == []
    await poll(cog, UP + [container("radarr")])
    assert len(alerts.resolved) == 1                                  # a healthy poll writes nothing more


@pytest.mark.asyncio
async def test_a_clean_stop_is_only_news_when_it_was_asked_for(tmp_path):
    stopped = UP + [container("pgbackup", "exited", "Exited (0) 2 hours ago")]
    quiet = make(tmp_path)
    await poll(quiet, stopped)
    assert quiet.bot.alerts.fired == []                               # someone stopped it on purpose

    loud = make(tmp_path / "loud", DOCKER_ALERT_ON_STOP="true")
    await poll(loud, stopped)
    (alert,) = loud.bot.alerts.fired
    assert alert.fingerprint == "docker:container:pgbackup:exited" and alert.severity is Severity.WARNING
    assert alert.title == "Container stopped: pgbackup"


@pytest.mark.asyncio
async def test_a_failing_health_check_fires_and_clears(tmp_path):
    cog = make(tmp_path)
    await poll(cog, [container("sonarr", status="Up 3 hours (unhealthy)")])
    (alert,) = cog.bot.alerts.fired
    assert alert.fingerprint == "docker:container:sonarr:unhealthy" and alert.severity is Severity.WARNING
    assert alert.title == "Health check failing: sonarr" and alert.fields["Up"] == "3h"
    await poll(cog, [container("sonarr", status="Up 4 hours (healthy)")])
    assert cog.bot.alerts.resolved == [("docker:container:sonarr:unhealthy", "`sonarr` is healthy again")]


@pytest.mark.asyncio
async def test_a_restart_loop_needs_n_restarts_inside_an_hour(tmp_path):
    cog = make(tmp_path, DOCKER_RESTART_LOOP_N=3)
    alerts = cog.bot.alerts
    await poll(cog, [container("immich-ml", status="Up 2 days")])
    for seconds in (5, 4, 3):
        await poll(cog, [container("immich-ml", status=f"Up {seconds} seconds")])
    assert [a.fingerprint for a in alerts.fired] == ["docker:container:immich-ml:restart_loop"]
    assert "restarted **3** times in the last hour" in alerts.fired[0].description
    assert len(cog._restarts["immich-ml"]) == 3

    # restarts older than the window stop counting, and the alert closes once none are left
    cog._restarts["immich-ml"] = [time.time() - RESTART_WINDOW_S - 60] * 3
    await poll(cog, [container("immich-ml", status="Up 2 days")])
    assert alerts.resolved == [("docker:container:immich-ml:restart_loop", "`immich-ml` has settled")]
    assert cog._restarts["immich-ml"] == []


def test_what_counts_as_a_restart():
    up = parse_containers([container("x", status="Up 2 days")])[0]
    fresh = parse_containers([container("x", status="Up 5 seconds")])[0]
    down = parse_containers([container("x", "exited", "Exited (1) 1 second ago")])[0]
    looping = parse_containers([container("x", "restarting", "Restarting (1) 2 seconds ago")])[0]
    assert not restarted("", fresh, 60)                    # first sight of a container proves nothing
    assert not restarted("running", up, 60)
    assert restarted("running", fresh, 60)                 # up for less than a poll: it came back in between
    assert not restarted("running", fresh, 2)              # ... unless the poll is quicker than that
    assert restarted("exited", up, 60) and not restarted("running", down, 60)
    assert restarted("running", looping, 60) and not restarted("restarting", looping, 60)


@pytest.mark.asyncio
async def test_an_unreachable_daemon_is_one_critical_alert(tmp_path):
    cog = make(tmp_path)
    alerts = cog.bot.alerts
    cog.bot.docker.error = OSError("Cannot connect to host /var/run/docker.sock")
    for _ in range(MAX_FAILURES + 2):
        await poll(cog)
    (alert,) = alerts.fired                                # fired once, at the third failure, not on every poll
    assert alert.fingerprint == FP_UNREACHABLE and alert.severity is Severity.CRITICAL
    assert "3 consecutive polls of the Docker socket at /var/run/docker.sock failed" in alert.description
    assert "Cannot connect to host" in alert.description
    cog.bot.docker.error = None
    await poll(cog, UP)
    assert alerts.resolved == [(FP_UNREACHABLE, "The daemon is answering again")] and cog._failures == 0


@pytest.mark.asyncio
async def test_ignored_containers_are_silent_and_removed_ones_are_closed(tmp_path):
    cog = make(tmp_path, DOCKER_IGNORE="buildx_*,*-test")
    alerts = cog.bot.alerts
    noisy = [container("buildx_buildkit_default", "exited", "Exited (2) 1 minute ago"),
             container("nginx-test", status="Up 1 hour (unhealthy)")]
    await poll(cog, UP + noisy)
    assert alerts.fired == []

    await poll(cog, UP + noisy + [container("radarr", "exited", "Exited (137) 6 minutes ago")])
    assert alerts.active() == ["docker:container:radarr:exited"]
    await poll(cog, UP)                                    # the container was removed from the host altogether
    assert alerts.resolved == [("docker:container:radarr:exited", "`radarr` no longer exists on this host")]
    assert alerts.active() == []


@pytest.mark.asyncio
async def test_image_updates_are_one_info_alert_that_follows_the_list(tmp_path):
    cog = make(tmp_path, DOCKER_CHECK_UPDATES="true")
    alerts = cog.bot.alerts
    cog.bot.docker.update_rows = [{"ref": ref, "local": "", "remote": "sha256:new"} for ref in samples.UPDATES]
    await poll(cog, UP)
    (alert,) = alerts.fired
    assert alert.fingerprint == FP_UPDATES and alert.severity is Severity.INFO and alert.mention is False
    assert alert.title == "2 images have updates" and "`traefik:v3.1`" in alert.description
    assert cog._updates == samples.UPDATES

    await poll(cog, UP)                                    # nothing changed, and the check is not due again
    assert len(alerts.fired) == 1 and cog.bot.docker.update_rows

    cog._checked_updates = 0                               # the next check finds one more image
    cog.bot.docker.update_rows.append({"ref": "nginx:1.27", "local": "", "remote": "sha256:new"})
    await poll(cog, UP)
    assert [a.title for a in alerts.fired] == ["2 images have updates", "3 images have updates"]

    cog._checked_updates = 0
    cog.bot.docker.update_rows = []
    await poll(cog, UP)
    assert alerts.resolved == [(FP_UPDATES, "every image is current")] and cog.state.get("updates") == []
