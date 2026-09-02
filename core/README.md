# periscope core

Shared Python library (`periscope`) that every periscope bot is built on. It gives each bot the same look, the same config surface, and the same plumbing so you only write the integration.

## What's in the box

| Module | What it gives you |
|---|---|
| `periscope.LabBot` | `discord.py` Bot subclass: loads cogs, syncs slash commands to your guild, optional inbound webhook server, `/health` for Docker |
| `periscope.Settings` | All common env vars (`DISCORD_TOKEN`, `LAB_NAME`, `GUILD_ID`, `ALERT_CHANNEL_ID`, …) parsed once |
| `periscope.lab_embed` / `Severity` | Consistent colored embeds with lab-name footer and timestamp |
| `periscope.AlertRouter` | Fire alerts with dedupe + cooldown, `resolve()` edits the original message green, pings a role on CRITICAL |
| `periscope.StatusBoard` | One pinned message per channel the bot edits in place on a timer (a live dashboard) |
| `periscope.ConfirmView` / `PaginatorView` / `RefreshView` | Buttons you'll want in every bot |
| `periscope.HttpClient` | aiohttp wrapper with auth, timeouts, self-signed TLS toggle |
| `periscope.WebhookServer` | Receive Alertmanager / GitHub / *arr webhooks with shared-secret or HMAC auth |
| `periscope.JsonState` | Tiny persistent JSON store under `DATA_DIR` |

## Install

Installed by the pack's `setup.sh`. For development: `pip install -e core` from the repo root.

## Minimal bot

```python
from periscope import LabBot, Settings

settings = Settings.from_env()
bot = LabBot(settings, cogs=["mybot.cogs.status"], webhook=True)
bot.run_forever()
```

```python
# mybot/cogs/status.py
import discord
from discord import app_commands
from discord.ext import commands, tasks
from periscope import LabBot, StatusBoard, Severity, lab_embed

class Status(commands.Cog):
    def __init__(self, bot: LabBot):
        self.bot = bot
        self.board = StatusBoard(bot, key="mybot")
        self.tick.start()

    @tasks.loop(seconds=60)
    async def tick(self):
        e = lab_embed("My Lab", "all good", severity=Severity.OK, lab_name=self.bot.lab_name)
        await self.board.render(e)

    @tick.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @app_commands.command(description="Ping the bot")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"pong from {self.bot.lab_name}", ephemeral=True)

async def setup(bot: LabBot):
    await bot.add_cog(Status(bot))
```

## Common environment variables

| Var | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token from the Discord Developer Portal |
| `LAB_NAME` | | Shown in every embed footer, e.g. `chr0nu5-lab` |
| `LAB_COLOR` | | Hex color for the lab, e.g. `00D4FF` |
| `GUILD_ID` | | Server id. Set it — commands sync instantly instead of taking an hour |
| `ALERT_CHANNEL_ID` | | Where `AlertRouter` posts |
| `STATUS_CHANNEL_ID` | | Where `StatusBoard` keeps its live message |
| `ALERT_ROLE_ID` | | Role pinged on CRITICAL alerts |
| `ADMIN_ROLE_IDS` | | Comma-separated role ids allowed to run destructive commands |
| `DATA_DIR` | | Persistent state dir (default `data`, relative to the working directory; `/data` in the Docker images) |
| `WEBHOOK_PORT` / `WEBHOOK_SECRET` | | Inbound webhook listener (if the bot enables it) |
| `LOG_LEVEL` | | `INFO` by default |

## Multi-lab model

Every member self-hosts each bot they want, pointed at their own lab, all posting into the shared server. `LAB_NAME` is what tells them apart. No inbound access to anyone's lab is needed — the bot only makes outbound connections to Discord and to the local services it monitors.

## License

MIT
