"""Webhook receiver: POST /github  (one org-level GitHub webhook, HMAC via X-Hub-Signature-256)."""

from __future__ import annotations

import json
import logging

from aiohttp import web
from discord.ext import commands
from periscope import LabBot

from ..dispatch import get_dispatcher
from ..render import RENDERERS

log = logging.getLogger(__name__)


class GithubEvents(commands.Cog):
    def __init__(self, bot: LabBot):
        self.bot = bot
        self.dispatcher = get_dispatcher(bot)
        if bot.webhook is None:
            log.warning("webhook server disabled; /github route not registered")
            return
        if not bot.webhook.secret:
            log.warning("WEBHOOK_SECRET is not set: /github will accept UNSIGNED payloads. Set it (and the same "
                        "secret on GitHub) before exposing the bot to the internet.")
        # WebhookServer.add_route wraps the handler with bot.webhook.authorized() (HMAC check over the raw body).
        bot.webhook.add_route("POST", "/github", self.handle)
        log.info("registered POST /github (events: %s)", ", ".join(sorted(RENDERERS)))

    async def handle(self, request: web.Request) -> web.StreamResponse:
        event = request.headers.get("X-GitHub-Event", "").lower()
        delivery = request.headers.get("X-GitHub-Delivery")
        body = await request.read()
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "payload must be an object"}, status=400)

        if event == "ping":
            hook_url = ((payload.get("hook") or {}).get("config") or {}).get("url", "?")
            log.info("webhook ping from %s (zen: %s)", hook_url, payload.get("zen", ""))
            return web.json_response({"ok": True, "pong": True})
        if not event:
            return web.json_response({"error": "missing X-GitHub-Event"}, status=400)
        if event not in RENDERERS:
            log.debug("unhandled GitHub event %s (delivery %s)", event, delivery)
            return web.json_response({"ok": True, "ignored": event})

        posted = await self.dispatcher.dispatch(event, payload, delivery_id=delivery, source="webhook")
        return web.json_response({"ok": True, "posted": posted})


async def setup(bot: LabBot) -> None:
    await bot.add_cog(GithubEvents(bot))
