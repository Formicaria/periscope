"""Alert routing: one card per problem, and everything that happens to it afterwards.

A service calls `bot.alerts.fire(Alert(...))` whenever something needs attention and `bot.alerts.resolve(fp)`
when it stops. This module decides what that means:

* **One card per fingerprint.** A repeat of an alert that is already on the board edits that card and bumps a
  "seen N times" counter instead of posting a second copy.
* **Ack · Snooze · Resolve.** Every card carries admin-only controls. Acking stops the pings and writes who
  did it onto the card; snoozing holds the pings for 1, 8 or 24 hours; resolving closes the alert by hand
  exactly the way the service closes it by itself. The buttons are persistent — their ids never change, so
  they still work after a restart, and the answer to "is this acked?" lives in `bot.state`, not in memory.
* **Escalation.** A CRITICAL alert nobody acks within `ALERT_ESCALATE_MIN` minutes pings the alert role once
  more, and says so on the card. Zero (the default) switches it off.
* **Maintenance windows.** While `bot.windows` says the lab is meant to be quiet, nothing is posted at all —
  the suppression is logged and recorded, and if a card for that alert is already up it says the alert would
  have fired. A window config that cannot be read keeps nobody quiet: alerts go out.

Nothing here may raise into a send site. A missing channel, a deleted message, a broken template, a bad
window file — each is logged and the alert still gets the best treatment available.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import discord

from .embeds import Severity, lab_embed, truncate
from .messages import MessageKind, register
from .views import SNOOZE_HOURS, AlertActionView

if TYPE_CHECKING:
    from .bot import LabBot

log = logging.getLogger(__name__)

ALERT_KIND, RESOLVED_KIND = "core.alert", "core.alert_resolved"
STATUS_FIELD = "Status"                 # the one embed field this module owns and rewrites
DEFAULT_ESCALATE_MIN = 0                # 0 = never escalate
MAX_SEEN_SHOWN = 999


@dataclass
class Alert:
    fingerprint: str
    title: str
    description: str = ""
    severity: Severity = Severity.WARNING
    fields: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    mention: bool | None = None  # None → mention only on CRITICAL

    def to_embed(self, lab_name: str, resolved: bool = False) -> discord.Embed:
        sev = Severity.OK if resolved else self.severity
        title = f"RESOLVED: {self.title}" if resolved else self.title
        e = lab_embed(title, truncate(self.description, 4000) or None, severity=sev, lab_name=lab_name, url=self.url)
        for k, v in list(self.fields.items())[:25]:
            e.add_field(name=truncate(str(k), 256), value=truncate(str(v) or "—", 1024), inline=True)
        return e


# ----- settings ---------------------------------------------------------------------------------------------
def escalate_minutes(bot: Any) -> int:
    """`ALERT_ESCALATE_MIN`, a setting every service shares: minutes an unacked CRITICAL waits before the alert
    role is pinged again. Read from the service's own settings first, then its environment, then the process's,
    so it can be set per service or once for the whole install. Anything unreadable means "off"."""
    env = getattr(bot, "env", None)
    candidates = [getattr(getattr(bot, "settings", None), "alert_escalate_min", None),
                  env.get("ALERT_ESCALATE_MIN") if isinstance(env, dict) else None,
                  os.environ.get("ALERT_ESCALATE_MIN")]
    for raw in candidates:
        if raw is None or str(raw).strip() == "":
            continue
        try:
            return max(0, int(str(raw).split("#")[0].strip()))
        except (TypeError, ValueError):
            log.warning("ALERT_ESCALATE_MIN is not a number (%r) — escalation stays off", raw)
            return DEFAULT_ESCALATE_MIN
    return DEFAULT_ESCALATE_MIN


# ----- what the card says ----------------------------------------------------------------------------------
def _clock(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        return dt.datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except (ValueError, OSError, OverflowError):
        return ""


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def status_lines(record: dict[str, Any]) -> list[str]:
    """The plain-language state of one alert, newest fact last. This is what the Status field holds."""
    lines: list[str] = []
    seen = int(record.get("count") or 1)
    if seen > 1:
        first = _clock(record.get("first_ts")) or "it started"
        lines.append(f"🔁 Seen {min(seen, MAX_SEEN_SHOWN)} times since {first}")
    if record.get("acked_ts"):
        who = record.get("acked_name") or "an admin"
        lines.append(f"✅ Acked by {who} at {_clock(record.get('acked_ts'))} — no more pings")
    if record.get("snooze_until"):
        who = record.get("snoozed_by") or "an admin"
        lines.append(f"😴 Snoozed by {who} until {_clock(record.get('snooze_until'))}")
    if record.get("escalated_ts"):
        minutes = int(record.get("escalate_min") or 0)
        after = f" after {_plural(minutes, 'minute')}" if minutes else ""
        lines.append(f"📣 Nobody acked it{after} — the alert role was pinged again")
    if record.get("suppressed"):
        why = record.get("suppressed_reason") or "a maintenance window is open"
        lines.append(f"🔕 It fired again during a quiet time, so nothing was sent — {why}")
    if not lines:
        lines.append("🔔 Firing — nobody has acked it yet")
    return lines


def stamp_status(embed: discord.Embed, record: dict[str, Any]) -> discord.Embed:
    """Put (or replace) the Status field on a card. Everything else on the embed is left alone."""
    text = truncate("\n".join(status_lines(record)), 1024)
    for i, existing in enumerate(embed.fields):
        if existing.name == STATUS_FIELD:
            embed.set_field_at(i, name=STATUS_FIELD, value=text, inline=False)
            return embed
    if len(embed.fields) >= 25:
        embed.remove_field(0)
    embed.add_field(name=STATUS_FIELD, value=text, inline=False)
    return embed


# ----- the router -------------------------------------------------------------------------------------------
class AlertRouter:
    """Sends alerts to the configured alert channel, one card per fingerprint, and runs their lifecycle.

    - Repeat alerts with the same fingerprint edit the open card and bump its "seen N times" counter.
    - Ack / Snooze / Resolve live on the card, are admin-only, and survive a restart.
    - An unacked CRITICAL escalates once after `ALERT_ESCALATE_MIN` minutes.
    - Nothing is sent at all while `bot.windows` says a maintenance window is open.
    - `resolve(fingerprint)` edits the original message to green and clears state.
    """

    def __init__(self, bot: "LabBot", cooldown_s: int = 300):
        self.bot = bot
        self.cooldown_s = cooldown_s
        self.scope = str(getattr(bot, "name", "") or "core")
        self._state = bot.state.namespace("alerts")
        self._last_sent: dict[str, float] = {}
        self._view: AlertActionView | None = None
        self._registered = False
        self._tasks: set[asyncio.Task] = set()
        try:                        # re-arm the card buttons after every reconnect, without touching the bot
            self.bot.add_listener(self._on_ready, "on_ready")
        except Exception:  # noqa: BLE001 - a bot that has no listeners (a test double) is fine
            log.debug("[%s] alert cards: could not hook on_ready", self.scope, exc_info=True)

    # ----- plumbing -----------------------------------------------------------------------------------
    async def _channel(self) -> discord.abc.Messageable | None:
        cid = self.bot.settings.alert_channel_id
        if not cid:
            log.warning("ALERT_CHANNEL_ID not set; dropping alert")
            return None
        return await self.bot.get_channel_safe(cid)

    def _get(self, fingerprint: str) -> dict[str, Any]:
        raw = self._state.get(fingerprint)
        return dict(raw) if isinstance(raw, dict) else {}

    def _put(self, fingerprint: str, record: dict[str, Any]) -> None:
        self._state.set(fingerprint, record)

    async def _on_ready(self) -> None:
        """Every reconnect: re-arm the card buttons, and catch up on anything that fell due while we were away."""
        self.register_views()
        try:
            await self.tick()
        except Exception:  # noqa: BLE001 - a bot that has just come up must not fall over on old state
            log.exception("[%s] could not catch up on escalations", self.scope)

    def register_views(self) -> None:
        """Hand Discord the one view that answers clicks on every card this service ever posted."""
        if self._registered:
            return
        try:
            self.bot.add_view(self.persistent_view())
            self._registered = True
        except Exception:  # noqa: BLE001 - a bot that cannot take views still sends alerts
            log.debug("[%s] alert cards: add_view failed", self.scope, exc_info=True)

    def persistent_view(self) -> AlertActionView:
        if self._view is None:
            self._view = AlertActionView(self, scope=self.scope)
        return self._view

    def card_view(self, record: dict[str, Any]) -> AlertActionView:
        """A throwaway copy of the view dressed for one alert (greyed-out Ack once it is acked, and so on)."""
        return AlertActionView(self, scope=self.scope, state=record)

    def close(self) -> None:
        """Drop any pending escalation timers (a service being unloaded, or a test finishing)."""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    # ----- maintenance windows -------------------------------------------------------------------------
    @property
    def server_name(self) -> str:
        return str(getattr(getattr(self.bot, "settings", None), "lab_name", "") or "")

    def quiet_reason(self, alert: Alert) -> str:
        """Why this alert must not be sent right now, or "". A broken window config keeps nobody quiet."""
        windows = getattr(self.bot, "windows", None)
        if windows is None:
            return ""
        try:
            if not windows.quiet(self.scope, server=self.server_name, key=alert.fingerprint):
                return ""
            said = windows.reason(self.scope, server=self.server_name, key=alert.fingerprint)
            return said or "a maintenance window is open"
        except Exception:  # noqa: BLE001 - fail open: a window we cannot read must not swallow an alert
            log.exception("[%s] maintenance windows failed — alerting anyway", self.scope)
            return ""

    # ----- history ------------------------------------------------------------------------------------
    def _remember(self, fingerprint: str, record: dict[str, Any], action: str, detail: str = "") -> None:
        history = getattr(self.bot, "history", None)
        if history is None:
            return
        try:
            history.record(service=self.scope, kind="alert", key=fingerprint,
                           severity=str(record.get("severity") or Severity.WARNING.value),
                           title=str(record.get("title") or fingerprint),
                           detail=f"{action}{' — ' + detail if detail else ''}")
        except Exception:  # noqa: BLE001 - the log is a nicety; it never blocks an alert
            log.debug("[%s] history.record failed for %s", self.scope, fingerprint, exc_info=True)

    # ----- fire ----------------------------------------------------------------------------------------
    async def fire(self, alert: Alert, force: bool = False) -> discord.Message | None:
        now = time.time()
        record = self._get(alert.fingerprint)
        self._expire_snooze(record, now)

        reason = self.quiet_reason(alert)
        if reason:
            return await self._hold_back(alert, record, reason, now)

        if record.get("message_id"):
            msg = await self._repeat(alert, record, now)
            if msg is not None:
                return msg                      # the open card was edited; nothing new was posted
            record.pop("message_id", None)      # the card is gone from Discord: fall through and post one

        last = max(self._last_sent.get(alert.fingerprint, 0.0), float(record.get("ts") or 0.0))
        if not force and now - last < self.cooldown_s:
            log.debug("alert %s suppressed (cooldown)", alert.fingerprint)
            return None
        return await self._post(alert, record, now)

    async def _post(self, alert: Alert, record: dict[str, Any], now: float) -> discord.Message | None:
        ch = await self._channel()
        if ch is None:
            return None
        embed = alert.to_embed(self.bot.settings.lab_name)
        messages = getattr(self.bot, "messages", None)
        if messages is not None:
            embed = messages.apply(ALERT_KIND, embed, alert_ctx(alert))
            if embed is None:
                log.info("alert %s not posted: alerts are switched off on the Messages page", alert.fingerprint)
                return None

        record = self._bump(record, alert, now)
        record["suppressed"] = False
        record.pop("suppressed_reason", None)
        stamp_status(embed, record)
        content = self._ping_for(alert, record)
        self.register_views()
        msg = await ch.send(content=content, embed=embed, view=self.card_view(record),
                            allowed_mentions=discord.AllowedMentions(roles=True))
        record.update({"message_id": msg.id, "channel_id": msg.channel.id, "ts": now})
        self._arm_escalation(alert, record, now)
        self._put(alert.fingerprint, record)
        self._last_sent[alert.fingerprint] = now
        return msg

    async def _repeat(self, alert: Alert, record: dict[str, Any], now: float) -> discord.Message | None:
        """The same problem again while its card is up: count it, refresh the card, ping nobody."""
        record = self._bump(record, alert, now)
        record["suppressed"] = False
        record.pop("suppressed_reason", None)
        record["ts"] = now
        msg = await self._refresh_card(alert.fingerprint, record, alert=alert)
        if msg is None:
            return None
        self._put(alert.fingerprint, record)
        self._last_sent[alert.fingerprint] = now
        return msg

    async def _hold_back(self, alert: Alert, record: dict[str, Any], reason: str,
                         now: float) -> discord.Message | None:
        """A maintenance window is open: post nothing, say so in the log and the event history, and — if a card
        for this alert is already up — write on it that the alert fired again and was held back."""
        log.info("alert %s held back: %s", alert.fingerprint, reason)
        record = self._bump(record, alert, now)
        record.update({"suppressed": True, "suppressed_reason": reason, "suppressed_ts": now})
        self._remember(alert.fingerprint, record, "suppressed", reason)
        if not record.get("message_id"):
            return None                          # nothing was ever posted, so there is nothing to leave behind
        msg = await self._refresh_card(alert.fingerprint, record, alert=alert)
        self._put(alert.fingerprint, record)
        return msg

    def _bump(self, record: dict[str, Any], alert: Alert, now: float) -> dict[str, Any]:
        record = dict(record)
        record["count"] = int(record.get("count") or 0) + 1
        record.setdefault("first_ts", now)
        record["severity"] = alert.severity.value
        record["title"] = alert.title
        record["service"] = self.scope
        return record

    def _ping_for(self, alert: Alert, record: dict[str, Any]) -> str | None:
        """The role mention that goes with a fresh post — nothing once the alert is acked or snoozed."""
        if record.get("acked_ts") or record.get("snooze_until"):
            return None
        mention = alert.mention if alert.mention is not None else alert.severity is Severity.CRITICAL
        role = getattr(self.bot.settings, "alert_role_id", None)
        return f"<@&{role}>" if mention and role else None

    def _expire_snooze(self, record: dict[str, Any], now: float) -> bool:
        """A snooze that has run out re-arms the alert: the next fire pings again."""
        until = float(record.get("snooze_until") or 0.0)
        if until and until <= now:
            record.pop("snooze_until", None)
            record.pop("snoozed_by", None)
            record["snooze_expired_ts"] = now
            return True
        return False

    # ----- the card -----------------------------------------------------------------------------------
    async def _message(self, record: dict[str, Any]) -> discord.Message | None:
        cid, mid = record.get("channel_id"), record.get("message_id")
        if not cid or not mid:
            return None
        ch = await self.bot.get_channel_safe(int(cid))
        if ch is None:
            return None
        try:
            return await ch.fetch_message(int(mid))  # type: ignore[attr-defined]
        except discord.NotFound:
            return None
        except discord.HTTPException as e:
            log.warning("[%s] could not read alert card %s: %s", self.scope, mid, e)
            return None

    async def _refresh_card(self, fingerprint: str, record: dict[str, Any], *,
                            alert: Alert | None = None) -> discord.Message | None:
        """Rewrite the card in place from the record. Returns None when the card is no longer there."""
        msg = await self._message(record)
        if msg is None:
            return None
        embed = self._embed_for(msg, alert)
        if embed is None:
            return msg
        stamp_status(embed, record)
        quiet = bool(record.get("acked_ts") or record.get("snooze_until"))
        try:
            content = None if quiet else (msg.content or None)
            await msg.edit(content=content, embed=embed, view=self.card_view(record))
        except discord.HTTPException as e:
            log.warning("[%s] could not update alert card %s: %s", self.scope, record.get("message_id"), e)
        return msg

    def _embed_for(self, msg: discord.Message, alert: Alert | None) -> discord.Embed | None:
        """The embed to write back: a rebuilt one when we still hold the Alert, else the card's own."""
        if alert is not None:
            embed = alert.to_embed(self.bot.settings.lab_name)
            messages = getattr(self.bot, "messages", None)
            if messages is not None:
                embed = messages.apply(ALERT_KIND, embed, alert_ctx(alert)) or embed
            return embed
        if not msg.embeds:
            return None
        return discord.Embed.from_dict(msg.embeds[0].to_dict())

    # ----- escalation ---------------------------------------------------------------------------------
    def _arm_escalation(self, alert: Alert, record: dict[str, Any], now: float) -> None:
        minutes = escalate_minutes(self.bot)
        if minutes <= 0 or alert.severity is not Severity.CRITICAL:
            record.pop("escalate_at", None)
            return
        if record.get("escalated_ts") or record.get("acked_ts"):
            return
        record["escalate_min"] = minutes
        # a timer that is already running keeps running: a restart, or the same problem firing again, must not
        # push the deadline out and let an alert sit unacked forever
        record.setdefault("escalate_at", now + minutes * 60)
        self._schedule(float(record["escalate_at"]) - now)

    def _schedule(self, delay: float) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return                              # no loop (a synchronous test): `tick()` is the way in
        task = loop.create_task(self._sleep_then_tick(delay))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _sleep_then_tick(self, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            await self.tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed escalation must not take the service down
            log.exception("[%s] escalation check failed", self.scope)

    async def tick(self, now: float | None = None) -> list[str]:
        """Escalate every CRITICAL that is due. Safe to call as often as you like; each alert escalates once."""
        now = time.time() if now is None else now
        escalated: list[str] = []
        for fingerprint in self.active():
            record = self._get(fingerprint)
            due = float(record.get("escalate_at") or 0.0)
            if not due or due > now:
                continue
            if record.get("acked_ts") or record.get("escalated_ts") or record.get("snooze_until"):
                record.pop("escalate_at", None)
                self._put(fingerprint, record)
                continue
            await self._escalate(fingerprint, record, now)
            escalated.append(fingerprint)
        return escalated

    async def _escalate(self, fingerprint: str, record: dict[str, Any], now: float) -> None:
        minutes = int(record.get("escalate_min") or 0)
        record["escalated_ts"] = now
        record.pop("escalate_at", None)
        self._put(fingerprint, record)
        self._remember(fingerprint, record, "escalated", f"unacked for {_plural(minutes, 'minute')}")
        role = getattr(self.bot.settings, "alert_role_id", None)
        where = int(record.get("channel_id") or 0)
        ch = await self.bot.get_channel_safe(where) if where else None
        if ch is not None and role:
            title = record.get("title") or fingerprint
            try:
                await ch.send(content=f"<@&{role}> **{title}** is still open — nobody acked it in "
                                      f"{_plural(minutes, 'minute')}.",
                              allowed_mentions=discord.AllowedMentions(roles=True))
            except discord.HTTPException as e:
                log.warning("[%s] could not re-ping for %s: %s", self.scope, fingerprint, e)
        await self._refresh_card(fingerprint, record)
        log.warning("alert %s escalated: unacked for %s", fingerprint, _plural(minutes, "minute"))

    # ----- ack / snooze / resolve ----------------------------------------------------------------------
    async def ack(self, fingerprint: str, *, who: str = "", user_id: int | None = None) -> bool:
        """Stop the pings on an alert and write who did it onto the card."""
        record = self._get(fingerprint)
        if not record:
            return False
        now = time.time()
        record.update({"acked_ts": now, "acked_name": who or "an admin", "acked_by": user_id})
        record.pop("escalate_at", None)
        self._put(fingerprint, record)
        self._remember(fingerprint, record, "acked", f"by {who}" if who else "")
        await self._refresh_card(fingerprint, record)
        log.info("alert %s acked by %s", fingerprint, who or "an admin")
        return True

    async def snooze(self, fingerprint: str, hours: float = 1, *, who: str = "") -> bool:
        """Hold the pings for a while. When it runs out the alert re-arms and its next repeat pings again."""
        record = self._get(fingerprint)
        if not record:
            return False
        now = time.time()
        record.update({"snooze_until": now + max(0.0, float(hours)) * 3600, "snoozed_by": who or "an admin"})
        record.pop("escalate_at", None)
        record.pop("snooze_expired_ts", None)
        self._put(fingerprint, record)
        self._remember(fingerprint, record, "snoozed", f"for {hours}h" + (f" by {who}" if who else ""))
        await self._refresh_card(fingerprint, record)
        log.info("alert %s snoozed for %sh by %s", fingerprint, hours, who or "an admin")
        return True

    async def resolve(self, fingerprint: str, note: str | None = None, *, by: str = "") -> bool:
        self._last_sent.pop(fingerprint, None)
        record = self._get(fingerprint)
        if not record:
            return False                        # nothing to resolve; avoid a state write
        self._state.pop(fingerprint)
        self._remember(fingerprint, record, "resolved", note or (f"by {by}" if by else ""))
        msg = await self._message(record)
        if msg is None or not msg.embeds:
            return False
        old = msg.embeds[0]
        e = discord.Embed.from_dict(old.to_dict())
        e.color = Severity.OK.color
        bare = old.title.split(" ", 1)[-1] if old.title else ""
        e.title = f"🟢 RESOLVED: {bare}".strip()
        for i, existing in enumerate(e.fields):
            if existing.name == STATUS_FIELD:
                e.remove_field(i)
                break
        if note:
            e.add_field(name="Resolution", value=truncate(note, 1024), inline=False)
        if by:
            e.add_field(name="Closed by", value=truncate(by, 1024), inline=False)
        messages = getattr(self.bot, "messages", None)
        if messages is not None:
            e = messages.apply(RESOLVED_KIND, e, {"alert_title": bare, "note": note or "", "severity": "ok",
                                                  "fingerprint": fingerprint}) or e
        try:
            await msg.edit(content=None, embed=e, view=None)
        except discord.HTTPException as err:
            log.warning("[%s] could not close alert card %s: %s", self.scope, record.get("message_id"), err)
            return False
        return True

    # ----- what the buttons call -----------------------------------------------------------------------
    def can_act(self, user: Any) -> bool:
        try:
            return bool(self.bot.is_admin(user))
        except Exception:  # noqa: BLE001
            log.exception("[%s] alert card: admin check failed", self.scope)
            return False

    def by_message(self, message_id: int | None) -> str:
        """Which alert a card belongs to. The message id is the key, so the buttons need no id of their own."""
        if not message_id:
            return ""
        for fingerprint in self.active():
            if int(self._get(fingerprint).get("message_id") or 0) == int(message_id):
                return fingerprint
        return ""

    @staticmethod
    def _who(interaction: discord.Interaction) -> str:
        user = getattr(interaction, "user", None)
        return str(getattr(user, "display_name", None) or getattr(user, "name", None) or "an admin")

    async def _from_card(self, interaction: discord.Interaction) -> str:
        fingerprint = self.by_message(getattr(getattr(interaction, "message", None), "id", None))
        if not fingerprint:
            await interaction.response.send_message("That alert is already closed — nothing to do.",
                                                    ephemeral=True)
        return fingerprint

    async def on_ack(self, interaction: discord.Interaction) -> None:
        fingerprint = await self._from_card(interaction)
        if not fingerprint:
            return
        user = getattr(interaction, "user", None)
        await interaction.response.defer()
        await self.ack(fingerprint, who=self._who(interaction), user_id=getattr(user, "id", None))
        await interaction.followup.send("Acked — the pings stop here.", ephemeral=True)

    async def on_snooze(self, interaction: discord.Interaction, hours: float) -> None:
        fingerprint = await self._from_card(interaction)
        if not fingerprint:
            return
        await interaction.response.defer()
        await self.snooze(fingerprint, hours, who=self._who(interaction))
        await interaction.followup.send(f"Snoozed for {int(hours)}h — it will speak up again after that.",
                                        ephemeral=True)

    async def on_resolve(self, interaction: discord.Interaction) -> None:
        fingerprint = await self._from_card(interaction)
        if not fingerprint:
            return
        who = self._who(interaction)
        await interaction.response.defer()
        await self.resolve(fingerprint, note=f"Closed by hand by {who}", by=who)
        await interaction.followup.send("Closed.", ephemeral=True)

    # ----- what is open -------------------------------------------------------------------------------
    def active(self) -> list[str]:
        # `bot.state` is the root JsonState for a v1 LabBot but a NamespacedState for a v2 ServiceBot;
        # the alerts namespace always hangs off the root, so walk to it from there.
        prefix = self._state._prefix
        return [k[len(prefix):] for k in list(self._state._p._data) if k.startswith(prefix)]

    def snapshot(self, now: float | None = None) -> list[dict[str, Any]]:
        """Every open alert with its state, worst first — what the /alerts page and the CLI show."""
        now = time.time() if now is None else now
        order = {Severity.CRITICAL.value: 0, Severity.WARNING.value: 1, Severity.INFO.value: 2}
        rows = []
        for fingerprint in self.active():
            record = self._get(fingerprint)
            if not record:
                continue
            snoozed = float(record.get("snooze_until") or 0.0)
            rows.append({
                "fingerprint": fingerprint, "service": record.get("service") or self.scope,
                "title": record.get("title") or fingerprint, "severity": record.get("severity") or "warning",
                "count": int(record.get("count") or 1), "since": record.get("first_ts") or record.get("ts"),
                "acked_by": record.get("acked_name") if record.get("acked_ts") else "",
                "acked_ts": record.get("acked_ts"), "snoozed_until": snoozed if snoozed > now else None,
                "snoozed_by": record.get("snoozed_by") if snoozed > now else "",
                "escalated": bool(record.get("escalated_ts")), "suppressed": bool(record.get("suppressed")),
                "suppressed_reason": record.get("suppressed_reason") or "",
                "state": self.state_word(record, now), "lines": status_lines(record),
            })
        rows.sort(key=lambda r: (order.get(str(r["severity"]), 3), -(r["since"] or 0)))
        return rows

    @staticmethod
    def state_word(record: dict[str, Any], now: float) -> str:
        """One word for a badge: firing · acked · snoozed · held back."""
        if float(record.get("snooze_until") or 0.0) > now:
            return "snoozed"
        if record.get("suppressed"):
            return "held back"
        if record.get("acked_ts"):
            return "acked"
        return "firing"


# ----- message kinds (Messages page) ------------------------------------------------------------------------
def alert_ctx(alert: Alert, resolved: bool = False) -> dict:
    return {"alert_title": alert.title, "severity": "ok" if resolved else alert.severity.value, "fingerprint": alert.fingerprint,
            "note": "", "extra": dict(alert.fields)}


def _sample_alert() -> tuple[discord.Embed, dict]:
    a = Alert(fingerprint="pve:node:pve1:cpu", title="High CPU on pve1", description="CPU at **93%** for 3 polls (threshold 85%).",
              severity=Severity.WARNING, fields={"Node": "pve1", "Load": "12.4 / 8 cores"}, url="https://pve.example:8006")
    e = a.to_embed("my-lab")
    stamp_status(e, {"count": 3, "first_ts": time.time() - 900})
    return e, alert_ctx(a)


def _sample_resolved() -> tuple[discord.Embed, dict]:
    a = Alert(fingerprint="pve:node:pve1:cpu", title="High CPU on pve1", description="CPU at **93%** for 3 polls (threshold 85%).",
              severity=Severity.WARNING, fields={"Node": "pve1", "Load": "12.4 / 8 cores"})
    e = a.to_embed("my-lab", resolved=True)
    e.title = "🟢 RESOLVED: High CPU on pve1"
    e.add_field(name="Resolution", value="CPU back to 41%", inline=False)
    return e, {**alert_ctx(a, resolved=True), "note": "CPU back to 41%"}


register(
    MessageKind("core.alert", "Alert", "posted by every service when something needs attention (CPU, offline, stalled, …); "
                "edited in place when it repeats, is acked or snoozed, and again when it resolves",
                where="the alert channel", where_env="ALERT_CHANNEL_ID",
                sample=_sample_alert, group="alerts",
                variables={"alert_title": "the alert's title without the severity dot", "severity": "ok · info · warning · critical",
                           "fingerprint": "what makes this alert unique", "extra": "the alert's own fields as a map"}),
    MessageKind("core.alert_resolved", "Alert resolved", "how an alert message looks once its cause is gone",
                where="the alert channel (edited in place)", where_env="ALERT_CHANNEL_ID", sample=_sample_resolved, group="alerts",
                variables={"alert_title": "the alert's title", "note": "the resolution note, if any", "severity": "always ok"}),
)

__all__ = ["Alert", "AlertRouter", "ALERT_KIND", "RESOLVED_KIND", "SNOOZE_HOURS", "alert_ctx",
           "escalate_minutes", "stamp_status", "status_lines"]
