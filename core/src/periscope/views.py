"""Reusable interactive components."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Sequence

import discord

log = logging.getLogger(__name__)


class ConfirmView(discord.ui.View):
    """Yes/No buttons restricted to the invoking user. `await view.wait()` then check `view.value`."""

    def __init__(self, user_id: int, *, timeout: float = 30, danger: bool = True):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.value: bool | None = None
        self.confirm.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your button.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.value = True
        for c in self.children:
            c.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.value = False
        for c in self.children:
            c.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()


class PaginatorView(discord.ui.View):
    """Page through a list of embeds with ◀ ▶ buttons."""

    def __init__(self, pages: Sequence[discord.Embed], *, user_id: int | None = None, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.pages = list(pages)
        self.index = 0
        self.user_id = user_id
        self._sync()

    def _sync(self):
        self.prev.disabled = self.index <= 0
        self.next.disabled = self.index >= len(self.pages) - 1
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.user_id and interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your paginator.", ephemeral=True)
            return False
        return True

    @property
    def current(self) -> discord.Embed:
        return self.pages[self.index]

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.current, view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, _: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.current, view=self)


class RefreshView(discord.ui.View):
    """Persistent 🔄 button. `builder()` returns a fresh embed (or list of embeds)."""

    def __init__(self, builder: Callable[[], Awaitable[discord.Embed | list[discord.Embed]]], *,
                 custom_id: str = "periscope:refresh"):
        super().__init__(timeout=None)
        self._builder = builder
        self.refresh.custom_id = custom_id

    @discord.ui.button(emoji="🔄", label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        result = await self._builder()
        if isinstance(result, list):
            await interaction.edit_original_response(embeds=result, view=self)
        else:
            await interaction.edit_original_response(embed=result, view=self)


# ----- alert cards ------------------------------------------------------------------------------------------
ALERT_PREFIX = "periscope:alert"
SNOOZE_HOURS = (1, 8, 24)


def alert_custom_id(action: str, scope: str = "") -> str:
    """The custom_id an alert card's control carries. It names the action and the service whose router owns
    the card, and nothing else — the alert itself is found from the message the click came from, so the id
    stays the same for the life of the install and the buttons keep working after a restart."""
    return f"{ALERT_PREFIX}:{action}:{scope or 'core'}"


class AlertActionView(discord.ui.View):
    """Ack · Snooze · Resolve under an alert card, admin-only, and persistent across restarts.

    One instance per service is handed to `bot.add_view()` and answers clicks on every card that service ever
    posted; a second, throwaway instance rides along with each card so its buttons can show that alert's own
    state (greyed out once it is acked, and so on). The router does the work:

        can_act(user) -> bool          say no to everyone who is not an admin
        on_ack / on_resolve(interaction)
        on_snooze(interaction, hours)
    """

    def __init__(self, router: Any, *, scope: str = "", state: dict[str, Any] | None = None):
        super().__init__(timeout=None)
        self.router = router
        self.scope = scope or getattr(router, "scope", "") or "core"
        self.ack.custom_id = alert_custom_id("ack", self.scope)
        self.resolve.custom_id = alert_custom_id("resolve", self.scope)
        self.snooze.custom_id = alert_custom_id("snooze", self.scope)
        self.snooze.options = [discord.SelectOption(label=f"Snooze {h} hour{'s' if h != 1 else ''}",
                                                    value=str(h), description=f"No more pings for {h}h")
                               for h in SNOOZE_HOURS]
        self.apply_state(state or {})

    def apply_state(self, state: dict[str, Any]) -> None:
        """Dress this copy of the view for one alert: an acked alert cannot be acked again, and so on."""
        if state.get("acked_ts"):
            self.ack.disabled = True
            self.ack.label = f"Acked by {state.get('acked_name') or 'an admin'}"[:80]
            self.ack.style = discord.ButtonStyle.secondary
        if state.get("snooze_until"):
            self.snooze.placeholder = "Snoozed — pick another length"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        try:
            allowed = bool(self.router.can_act(interaction.user))
        except Exception:  # noqa: BLE001 - a broken admin check must not wedge the card
            log.exception("alert card: the admin check failed")
            allowed = False
        if allowed:
            return True
        await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
        return False

    @discord.ui.button(label="Ack", emoji="✅", style=discord.ButtonStyle.primary, row=0)
    async def ack(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.router.on_ack(interaction)

    @discord.ui.button(label="Resolve", emoji="🟢", style=discord.ButtonStyle.success, row=0)
    async def resolve(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.router.on_resolve(interaction)

    @discord.ui.select(placeholder="Snooze…", min_values=1, max_values=1, row=1,
                       options=[discord.SelectOption(label=f"Snooze {h}h", value=str(h)) for h in SNOOZE_HOURS])
    async def snooze(self, interaction: discord.Interaction, select: discord.ui.Select):
        try:
            hours = int(select.values[0])
        except (IndexError, ValueError):
            hours = SNOOZE_HOURS[0]
        await self.router.on_snooze(interaction, hours)
