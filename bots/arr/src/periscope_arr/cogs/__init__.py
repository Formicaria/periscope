"""Shared `/arr` command group. Each cog registers its bound methods as subcommands."""

from __future__ import annotations

from typing import Awaitable, Callable

from discord import app_commands

arr_group = app_commands.Group(name="arr", description="*arr stack, download clients and media servers")


def register(bot, *commands: tuple[str, str, Callable[..., Awaitable[None]]]) -> None:
    """Add (name, description, bound coroutine) triples to /arr and attach the group to the tree once."""
    for name, description, callback in commands:
        if arr_group.get_command(name) is not None:
            arr_group.remove_command(name)
        arr_group.add_command(app_commands.Command(name=name, description=description, callback=callback))
    if bot.tree.get_command("arr") is None:
        bot.tree.add_command(arr_group)
