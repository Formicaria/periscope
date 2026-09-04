"""What `/docker …` answers, driven through the cog with a stand-in interaction."""

from types import SimpleNamespace

import pytest
from periscope import env_scope

from periscope_docker import samples
from periscope_docker.cogs import containers as containers_cog
from periscope_docker.cogs import docker as docker_group
from periscope_docker.cogs.containers import ContainersCog
from periscope_docker.config import DockerConfig
from periscope_docker.util import parse_containers


class FakeDocker:
    def __init__(self):
        self.cached = []
        self.actions = []

    async def containers(self, all_containers=True):
        self.cached = parse_containers(samples.CONTAINERS)
        return self.cached

    async def logs(self, cid, lines=50):
        return "\n".join(samples.LOG_LINES) + "\n"

    async def stats(self, cid):
        return samples.STATS["jellyfin"]

    async def inspect(self, cid):
        return {"RestartCount": 2, "State": {"Status": "running"}}

    async def updates(self, refs):
        return [{"ref": ref, "local": "", "remote": "sha256:new"} for ref in samples.UPDATES if ref in refs]

    async def action(self, cid, action):
        self.actions.append((cid, action))

    def find(self, query):
        return next((c for c in self.cached if c.name == query), None)


class FakeInteraction:
    """Enough of discord.Interaction for a slash command: defer, followup, and editing the first reply."""

    def __init__(self):
        self.user = SimpleNamespace(id=7, __str__=lambda self: "alice")
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self._done = False
        self.response = SimpleNamespace(defer=self._defer, send_message=self._send, is_done=lambda: self._done)
        self.followup = SimpleNamespace(send=self._send)

    async def _defer(self, ephemeral=False, thinking=False):
        self._done = True

    async def _send(self, content=None, *, embed=None, view=None, ephemeral=False, **kw):
        self._done = True
        self.sent.append({"content": content, "embed": embed, "view": view, "ephemeral": ephemeral})

    async def edit_original_response(self, **kw):
        self.edits.append(kw)

    @property
    def last(self) -> dict:
        return self.sent[-1]


def make(tmp_path=None, **env) -> ContainersCog:
    with env_scope({str(k): str(v) for k, v in env.items()}):
        cfg = DockerConfig.from_env()
    bot = SimpleNamespace(cfg=cfg, docker=FakeDocker(), lab_name="THE LAB")
    return ContainersCog(bot)


@pytest.mark.asyncio
async def test_ps_lists_filters_and_paginates():
    cog = make(DOCKER_IGNORE="buildx_*")
    i = FakeInteraction()
    await docker_group.get_command("ps").callback(cog, i)
    embed = i.last["embed"]
    assert i.last["ephemeral"] and embed.title == "Containers (6)"
    assert embed.author.name == "6 containers · page 1/1" and i.last["view"] is None
    assert embed.description.splitlines()[0].startswith("🔴 **radarr**")     # trouble sorts to the top

    i = FakeInteraction()
    await docker_group.get_command("ps").callback(cog, i, name="arr")
    assert i.last["embed"].title == "Containers (2) · “arr”"                 # a bare word matches anywhere
    assert "sonarr" in i.last["embed"].description and "radarr" in i.last["embed"].description

    i = FakeInteraction()
    await docker_group.get_command("ps").callback(cog, i, name="*arr", running_only=True)
    assert i.last["embed"].title == "Containers (1) · running · “*arr”"
    i = FakeInteraction()
    await docker_group.get_command("ps").callback(cog, i, name="nothing")
    assert i.last["embed"].description == "No container matches."


@pytest.mark.asyncio
async def test_logs_stats_and_updates_read_one_host():
    cog = make(DOCKER_IGNORE="buildx_*")
    i = FakeInteraction()
    await docker_group.get_command("logs").callback(cog, i, container="sonarr", lines=3)
    assert i.last["content"].startswith("**sonarr** · last 3 lines\n```\n[2026-09-02 21:14:03] Starting Sonarr")
    assert i.last["content"].endswith("Indexer NZBgeek is unavailable\n```") and i.last["ephemeral"]

    i = FakeInteraction()
    await docker_group.get_command("stats").callback(cog, i, container="jellyfin")
    fields = {f.name: f.value for f in i.last["embed"].fields}
    assert i.last["embed"].title == "🟢 jellyfin — linuxserver/jellyfin:10.9.11"
    assert fields["CPU"] == "24.0%" and fields["Memory"] == "1.1 GB / 8.0 GB" and fields["Uptime"] == "12d"
    assert fields["Network"] == "↓ 9.2 GB · ↑ 1.1 GB" and fields["Container"] == "`3f0a1c9d5e7b`"
    assert fields["Restarts"] == "2 since it was created"       # the only thing inspect is asked for

    i = FakeInteraction()                                                    # a stopped container has nothing to read
    await docker_group.get_command("stats").callback(cog, i, container="radarr")
    assert i.last["content"] == "**radarr** is `exited` — no statistics to read."
    i = FakeInteraction()                                                    # and an ignored one is not found at all
    await docker_group.get_command("logs").callback(cog, i, container="buildx_buildkit_default")
    assert i.last["content"].startswith("No watched container matches")

    i = FakeInteraction()
    await docker_group.get_command("updates").callback(cog, i)
    lines = i.last["embed"].description.splitlines()
    assert i.last["embed"].title == "Images with updates (2)"
    assert lines == ["⬆ `linuxserver/sonarr:4.0.9` — sonarr", "⬆ `traefik:v3.1` — traefik"]


@pytest.mark.asyncio
async def test_power_actions_wait_for_the_confirm_button(monkeypatch):
    cog = make(DOCKER_IGNORE="buildx_*")
    await cog._watched()                                                     # what a poll would have cached
    answers = [True]
    prompts = []

    async def fake_confirm(interaction, prompt, *, danger=True):
        prompts.append((prompt, danger))
        return answers[0]

    monkeypatch.setattr(containers_cog, "confirm", fake_confirm)

    i = FakeInteraction()
    await docker_group.get_command("restart").callback(cog, i, container="sonarr")
    assert prompts[-1] == ("Restart **sonarr** (`linuxserver/sonarr:4.0.9`)?", True)
    assert cog.bot.docker.actions == [(cog.bot.docker.find("sonarr").id, "restart")]
    assert i.edits[-1] == {"content": "✅ Restarted **sonarr** (`linuxserver/sonarr:4.0.9`).", "view": None}

    answers[0] = False                                                       # cancelled: nothing is sent to Docker
    i = FakeInteraction()
    await docker_group.get_command("stop").callback(cog, i, container="jellyfin")
    assert "Anything it is serving goes away" in prompts[-1][0] and len(cog.bot.docker.actions) == 1
    assert i.edits == []

    answers[0] = True                                                        # starting is not a destructive button
    i = FakeInteraction()
    await docker_group.get_command("start").callback(cog, i, container="radarr")
    assert prompts[-1][1] is False and cog.bot.docker.actions[-1][1] == "start"

    i = FakeInteraction()                                                    # an ignored container is never touched
    await docker_group.get_command("restart").callback(cog, i, container="buildx_buildkit_default")
    assert i.last["content"].startswith("No watched container matches") and len(cog.bot.docker.actions) == 2
