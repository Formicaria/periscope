"""/docker ps, restart, start, stop, logs, stats, updates."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from periscope import PaginatorView, Severity, human_bytes, human_duration, lab_embed, truncate
from periscope.bot import admin_only

from ..bot import DockerBot
from ..util import (
    Container,
    block_io,
    container_line,
    cpu_percent,
    images_in_use,
    memory,
    network,
    sort_key,
    tail,
    watched,
)
from . import attach, confirm, docker, paginate

log = logging.getLogger(__name__)

DEFAULT_LOG_LINES = 50
ACTION_WORDS = {"start": ("Start", "Started"), "stop": ("Stop", "Stopped"), "restart": ("Restart", "Restarted")}


class ContainersCog(commands.Cog):
    def __init__(self, bot: DockerBot):
        self.bot = bot

    # ----- helpers ------------------------------------------------------

    async def _watched(self) -> list[Container]:
        """The containers this service reports on, freshly fetched."""
        return [c for c in await self.bot.docker.containers() if self.bot.cfg.watches(c.name)]

    async def _autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        cache = [c for c in self.bot.docker.cached if self.bot.cfg.watches(c.name)]
        if not cache:
            try:
                cache = await self._watched()
            except Exception as e:
                log.debug("autocomplete fetch failed: %s", e)
                return []
        q = current.lower()
        out = []
        for c in sorted(cache, key=sort_key):
            if q and q not in c.name.lower() and q not in c.image.lower():
                continue
            out.append(app_commands.Choice(name=f"{c.dot} {c.name} ({c.tag})"[:100], value=c.name))
            if len(out) == 25:
                break
        return out

    async def _find(self, interaction: discord.Interaction, name: str) -> Container | None:
        """Resolve a name (or id prefix) to one watched container, answering the user when there is no match."""
        containers = await self._watched()
        found = next((c for c in containers if c.name.lower() == name.strip().lstrip("/").lower()), None)
        if found is None:
            # the fetch above refreshed the client's cache, so this looks at the same poll, id prefixes included
            found = self.bot.docker.find(name)
            if found is not None and not self.bot.cfg.watches(found.name):
                found = None
        if found is None:
            await interaction.followup.send(f"No watched container matches `{truncate(name, 60)}`.", ephemeral=True)
        return found

    async def _act(self, interaction: discord.Interaction, name: str, action: str) -> None:
        """Admin-gated container power action: confirm, run it, say what happened. Always ephemeral."""
        try:
            target = self.bot.docker.find(name)
            if target is None:
                target = next((c for c in await self._watched() if c.name.lower() == name.strip().lower()), None)
        except Exception as e:
            await interaction.response.send_message(f"❌ Docker API error: `{truncate(str(e), 300)}`", ephemeral=True)
            return
        if target is None or not self.bot.cfg.watches(target.name):
            await interaction.response.send_message(f"No watched container matches `{truncate(name, 60)}`.",
                                                    ephemeral=True)
            return
        verb, done = ACTION_WORDS[action]
        label = f"**{target.name}** (`{target.tag}`)"
        warn = " Anything it is serving goes away until it is started again." if action == "stop" else ""
        if not await confirm(interaction, f"{verb} {label}?{warn}", danger=action != "start"):
            return
        try:
            await self.bot.docker.action(target.id, action)
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ `{action}` failed for {label}: `{truncate(str(e), 300)}`", view=None)
            return
        log.info("%s requested %s for container %s (%s)", interaction.user, action, target.name, target.short_id)
        await interaction.edit_original_response(content=f"✅ {done} {label}.", view=None)

    # ----- commands -----------------------------------------------------

    @docker.command(name="ps", description="List containers")
    @app_commands.describe(name="Only containers whose name matches this (globs work, e.g. *arr)",
                           running_only="Hide containers that are not running")
    async def ps(self, interaction: discord.Interaction, name: str | None = None,
                 running_only: bool = False) -> None:
        await interaction.response.defer(ephemeral=True)
        containers = await self._watched()
        if name:
            pattern = name if any(ch in name for ch in "*?[") else f"*{name}*"
            containers = [c for c in containers if watched(c.name, [pattern], [])]
        if running_only:
            containers = [c for c in containers if c.running]
        containers.sort(key=sort_key)
        title = f"Containers ({len(containers)})" + (" · running" if running_only else "")
        if name:
            title += f" · “{truncate(name, 30)}”"
        pages = paginate(title, (container_line(c) for c in containers), lab_name=self.bot.lab_name, per_page=10,
                         empty="No container matches.")
        view = PaginatorView(pages, user_id=interaction.user.id) if len(pages) > 1 else None
        await interaction.followup.send(embed=pages[0], view=view, ephemeral=True)

    @docker.command(name="restart", description="Restart a container")
    @app_commands.describe(container="Container name")
    @admin_only()
    async def restart(self, interaction: discord.Interaction, container: str) -> None:
        await self._act(interaction, container, "restart")

    @docker.command(name="start", description="Start a stopped container")
    @app_commands.describe(container="Container name")
    @admin_only()
    async def start(self, interaction: discord.Interaction, container: str) -> None:
        await self._act(interaction, container, "start")

    @docker.command(name="stop", description="Stop a running container")
    @app_commands.describe(container="Container name")
    @admin_only()
    async def stop(self, interaction: discord.Interaction, container: str) -> None:
        await self._act(interaction, container, "stop")

    @docker.command(name="logs", description="Show the last lines a container logged")
    @app_commands.describe(container="Container name", lines="How many lines (default 50, at most 200)")
    async def logs(self, interaction: discord.Interaction, container: str,
                   lines: app_commands.Range[int, 1, 200] = DEFAULT_LOG_LINES) -> None:
        await interaction.response.defer(ephemeral=True)
        target = await self._find(interaction, container)
        if target is None:
            return
        body = tail(await self.bot.docker.logs(target.id, lines), lines)
        if not body:
            await interaction.followup.send(f"**{target.name}** has logged nothing.", ephemeral=True)
            return
        await interaction.followup.send(f"**{target.name}** · last {lines} lines\n```\n{body}\n```", ephemeral=True)

    @docker.command(name="stats", description="CPU, memory, network and disk use of one container")
    @app_commands.describe(container="Container name")
    async def stats(self, interaction: discord.Interaction, container: str) -> None:
        await interaction.response.defer(ephemeral=True)
        target = await self._find(interaction, container)
        if target is None:
            return
        if not target.running:
            await interaction.followup.send(f"**{target.name}** is `{target.state}` — no statistics to read.",
                                            ephemeral=True)
            return
        sample = await self.bot.docker.stats(target.id)
        cpu = cpu_percent(sample)
        used, limit = memory(sample)
        rx, tx = network(sample)
        read, write = block_io(sample)
        e = lab_embed(f"{target.name} — {target.tag}", container_line(target), severity=Severity.OK,
                      lab_name=self.bot.lab_name)
        e.add_field(name="CPU", value=f"{cpu:.1f}%" if cpu is not None else "—")
        e.add_field(name="Memory", value=f"{human_bytes(used)}" + (f" / {human_bytes(limit)}" if limit else ""))
        e.add_field(name="Uptime", value=human_duration(target.uptime_s))
        e.add_field(name="Network", value=f"↓ {human_bytes(rx)} · ↑ {human_bytes(tx)}")
        e.add_field(name="Block I/O", value=f"read {human_bytes(read)} · written {human_bytes(write)}")
        e.add_field(name="Container", value=f"`{target.short_id}`")
        restarts = await self._restart_count(target)
        if restarts is not None:
            e.add_field(name="Restarts", value=f"{restarts} since it was created")
        await interaction.followup.send(embed=e, ephemeral=True)

    async def _restart_count(self, target: Container) -> int | None:
        """How often the daemon has restarted this container, from its inspect record. Decoration: a failure
        here must not cost the reply."""
        try:
            return int((await self.bot.docker.inspect(target.id)).get("RestartCount") or 0)
        except Exception as e:
            log.debug("inspect for %s failed: %s", target.name, e)
            return None

    @docker.command(name="updates", description="Which images the registry has a newer digest for")
    async def updates(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        containers = await self._watched()
        found = await self.bot.docker.updates(images_in_use(containers))
        if not found:
            await interaction.followup.send("Every image in use is the one the registry serves. ✅", ephemeral=True)
            return
        by_image = {u["ref"]: [c.name for c in containers if c.image == u["ref"]] for u in found}
        lines = [f"⬆ `{ref}` — {', '.join(names) or 'not in use'}" for ref, names in by_image.items()]
        e = lab_embed(f"Images with updates ({len(found)})", truncate("\n".join(lines), 4000),
                      severity=Severity.INFO, lab_name=self.bot.lab_name)
        e.set_footer(text="Pull the new image and recreate the container to take it")
        await interaction.followup.send(embed=e, ephemeral=True)

    @restart.autocomplete("container")
    @start.autocomplete("container")
    @stop.autocomplete("container")
    @logs.autocomplete("container")
    @stats.autocomplete("container")
    async def container_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete(interaction, current)


async def setup(bot: DockerBot) -> None:
    cog = ContainersCog(bot)
    await bot.add_cog(cog)
    attach(bot, cog)
