"""Reusable interactive components."""

from __future__ import annotations

from typing import Awaitable, Callable, Sequence

import discord


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
