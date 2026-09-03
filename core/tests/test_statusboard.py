"""StatusBoard: one live message per board — remembered, else adopted from the channel (stale copies deleted),
posted only when there is nothing to adopt."""

from types import SimpleNamespace

import discord
import pytest

from periscope.embeds import lab_embed
from periscope.state import JsonState
from periscope.statusboard import StatusBoard, board_key_of, stamp, title_stem

ME = SimpleNamespace(id=777, name="periscope")
OTHER = SimpleNamespace(id=1, name="someone")


class Msg:
    _ids = 100

    def __init__(self, channel, embed=None, author=ME, pinned=False):
        Msg._ids += 1
        self.id = Msg._ids
        self.channel, self.author, self.pinned = channel, author, pinned
        self.embeds = [embed] if embed is not None else []
        self.edits, self.deleted = [], False

    async def edit(self, **kw):
        self.edits.append(kw)
        if kw.get("embed") is not None:
            self.embeds = [kw["embed"]]

    async def delete(self):
        self.deleted = True
        self.channel.messages.pop(self.id, None)

    async def pin(self, reason=None):
        self.pinned = True


class Channel:
    def __init__(self, cid=1):
        self.id = cid
        self.messages: dict[int, Msg] = {}
        self.sent: list[Msg] = []

    def add(self, embed, author=ME, pinned=False):
        m = Msg(self, embed, author, pinned)
        self.messages[m.id] = m
        return m

    async def send(self, content=None, *, embed=None, embeds=None, view=None, **kw):
        m = Msg(self, embed if embed is not None else (embeds or [None])[0])
        self.messages[m.id] = m
        self.sent.append(m)
        return m

    async def fetch_message(self, mid):
        if mid in self.messages:
            return self.messages[mid]
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"message": "Unknown Message", "code": 10008})

    async def pins(self):
        return [m for m in self.messages.values() if m.pinned]

    async def history(self, limit=100):
        for m in sorted(self.messages.values(), key=lambda m: m.id, reverse=True)[:limit]:
            yield m


def bot(tmp_path, channel):
    state = JsonState(tmp_path / "state.json")

    async def get_channel_safe(cid):
        return channel if cid == channel.id else None

    return SimpleNamespace(user=ME, state=state, settings=SimpleNamespace(status_channel_id=channel.id),
                           get_channel_safe=get_channel_safe)


def test_stamp_and_stem():
    e = lab_embed("Proxmox · homelab", "x", lab_name="lab1")
    stamp(e, "pve")
    assert e.footer.text == "🧪 lab1 · pve board"
    stamp(e, "pve")                                                  # idempotent
    assert e.footer.text == "🧪 lab1 · pve board"
    assert board_key_of(SimpleNamespace(embeds=[e])) == "pve"
    assert board_key_of(SimpleNamespace(embeds=[discord.Embed(title="x")])) is None
    assert title_stem("🟢 Proxmox · homelab") == "proxmox" and title_stem("Proxmox") == "proxmox"
    assert title_stem("🔴 UniFi — default") == "unifi" and title_stem("GitHub: Formicaria") == "github"
    assert title_stem("Monitoring status") == "monitoring status" and title_stem("Media stack") == "media stack"
    assert title_stem("Now playing · 2 streams") == "now playing"


@pytest.mark.asyncio
async def test_first_render_posts_and_pins_then_edits(tmp_path):
    ch = Channel()
    b = StatusBoard(bot(tmp_path, ch), key="pve")
    m = await b.render(lab_embed("Proxmox · homelab", "1", lab_name="lab1"))
    assert m is ch.sent[0] and m.pinned and b._state.get("message_id") == m.id
    assert m.embeds[0].footer.text == "🧪 lab1 · pve board"
    m2 = await b.render(lab_embed("🔴 Proxmox · homelab", "2", lab_name="lab1"))
    assert m2 is m and len(ch.sent) == 1 and m.edits                 # edited, not re-posted


@pytest.mark.asyncio
async def test_adopts_marked_board_and_deletes_copies(tmp_path):
    """Fresh state (new install, migrated state file): the newest earlier copy is reused, the rest deleted."""
    ch = Channel()
    old1 = ch.add(stamp(lab_embed("Proxmox · homelab", "old", lab_name="lab1"), "pve"), pinned=True)
    old2 = ch.add(stamp(lab_embed("🟡 Proxmox · homelab", "older", lab_name="lab1"), "pve"), pinned=True)
    other = ch.add(stamp(lab_embed("UniFi — default", "u", lab_name="lab1"), "unifi"), pinned=True)   # another board
    alert = ch.add(lab_embed("Node pve1 is offline", "!", lab_name="lab1"))                             # not a board
    theirs = ch.add(stamp(lab_embed("Proxmox · homelab", "fake", lab_name="lab1"), "pve"), author=OTHER)  # not ours
    b = StatusBoard(bot(tmp_path, ch), key="pve")
    m = await b.render(lab_embed("Proxmox · homelab", "new", lab_name="lab1"))
    assert m is old2 and old1.deleted and not ch.sent                # newest kept + edited, older copy deleted
    assert old2.edits and old2.embeds[0].description == "new"
    assert not other.deleted and not alert.deleted and not theirs.deleted
    assert b._state.get("message_id") == old2.id


@pytest.mark.asyncio
async def test_adopts_unmarked_board_by_title_stem(tmp_path):
    """Boards posted before the marker existed: same bot, same title stem, in the pins or recent history."""
    ch = Channel()
    v1 = ch.add(lab_embed("🟢 Proxmox · homelab", "v1", lab_name="lab1"), pinned=True)
    unrelated = ch.add(lab_embed("Now playing · 2 streams", "x", lab_name="lab1"))
    b = StatusBoard(bot(tmp_path, ch), key="pve")
    m = await b.render(lab_embed("Proxmox", "api error", lab_name="lab1"))   # even the degraded title matches
    assert m is v1 and not ch.sent and not unrelated.deleted
    assert v1.embeds[0].footer.text == "🧪 lab1 · pve board"       # carries the marker from now on


@pytest.mark.asyncio
async def test_remembered_message_keeps_but_stale_copies_go(tmp_path):
    """The state remembers a message (v2 already ran once) and older copies from v1 are still pinned: the
    remembered one is edited, the old ones deleted — on the first render only."""
    ch = Channel()
    v1 = ch.add(lab_embed("🟢 Proxmox · homelab", "v1", lab_name="lab1"), pinned=True)
    mine = ch.add(stamp(lab_embed("Proxmox · homelab", "v2", lab_name="lab1"), "pve"), pinned=True)
    b = StatusBoard(bot(tmp_path, ch), key="pve")
    b._state.set("message_id", mine.id)
    m = await b.render(lab_embed("Proxmox · homelab", "3", lab_name="lab1"))
    assert m is mine and v1.deleted and not ch.sent
    late = ch.add(stamp(lab_embed("Proxmox · homelab", "late", lab_name="lab1"), "pve"), pinned=True)
    await b.render(lab_embed("Proxmox · homelab", "4", lab_name="lab1"))
    assert not late.deleted                                          # the sweep ran once; no scan per tick


@pytest.mark.asyncio
async def test_remembered_message_gone_adopts_before_posting(tmp_path):
    ch = Channel()
    b = StatusBoard(bot(tmp_path, ch), key="pve")
    b._state.set("message_id", 424242)                              # points nowhere
    survivor = ch.add(stamp(lab_embed("Proxmox", "s", lab_name="lab1"), "pve"))
    m = await b.render(lab_embed("Proxmox", "n", lab_name="lab1"))
    assert m is survivor and not ch.sent
    # nothing to adopt at all → post once; a later disappearance posts again (once), no scan loop
    ch2 = Channel(2)
    b2 = StatusBoard(bot(tmp_path, ch2), key="unifi", channel_id=2)
    m1 = await b2.render(lab_embed("UniFi", "1", lab_name="lab1"))
    ch2.messages.pop(m1.id)
    m2 = await b2.render(lab_embed("UniFi", "2", lab_name="lab1"))
    assert m2 is not m1 and len(ch2.sent) == 2 and b2._state.get("message_id") == m2.id


@pytest.mark.asyncio
async def test_channel_without_pins_api_still_works(tmp_path):
    class Bare(Channel):
        pins = None
        history = None

    ch = Bare()
    b = StatusBoard(bot(tmp_path, ch), key="pve")
    m = await b.render(lab_embed("Proxmox", "1", lab_name="lab1"))
    assert m is ch.sent[0]
