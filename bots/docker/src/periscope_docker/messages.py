"""The docker bot's message kinds: the board it posts, registered for the Messages page with a sample to
preview and customise it from.

Registering here is what lists a kind on the page. The send site — `cogs/status.py` — hands each embed to the
core `StatusBoard`, which passes it through `bot.messages.apply(kind, embed, ctx)` with the same ctx the sample
returns here. Everything else this service posts (a container that exited, a failing health check, a restart
loop, an unreachable daemon, the image-update notice) goes out through `bot.alerts` and is customised as the
core `core.alert` kind, so none of those are listed again.
"""

from __future__ import annotations

from typing import Any

import discord
from periscope.messages import MessageKind, register

from . import samples
from .cogs.status import BOARD_KIND, board_ctx, board_embed

LAB = "my-lab"   # the lab name previews carry; a real post carries the bot's

BOARD_VARIABLES = {
    "version": "the Docker Engine version the daemon reports",
    "endpoint": "where the bot is talking to: the socket path, the tcp endpoint, or the Portainer environment",
    "counts": "how many containers there are: running · stopped · unhealthy · restarting · total",
    "containers": "every watched container in board order: item.name · item.image · item.state · item.health · "
                  "item.trouble (crashed · stopped · unhealthy · restarting, empty when it is fine) · "
                  "item.uptime_s · item.exit_code · item.cpu (percent) · item.mem (bytes) · item.line (the row "
                  "the bot would draw)",
    "down": "the names of the containers that are not running",
    "updates": "the image tags the registry has a newer digest for (empty unless DOCKER_CHECK_UPDATES is on)",
    "checking_updates": "true when DOCKER_CHECK_UPDATES is on",
}


def _sample_board() -> tuple[discord.Embed | None, dict[str, Any]]:
    data = board_ctx(samples.VERSION["Version"], samples.ENDPOINT, samples.containers(), samples.UPDATES,
                     checking_updates=True)
    return board_embed(data, LAB), data


register(
    MessageKind(BOARD_KIND, "Container board",
                "the pinned board: how many containers are running, a row per watched container with its state, "
                "image, uptime and load, and which images have updates; refreshed every STATUS_INTERVAL_S and by "
                "its 🔄 button",
                where="the status channel", where_env="STATUS_CHANNEL_ID", sample=_sample_board, group="boards",
                variables=BOARD_VARIABLES),
)
