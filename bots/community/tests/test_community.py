import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from periscope_community import messages as M
from periscope_community.book import MaintBook
from periscope_community.config import parse_resources
from periscope_community.diagnose import group, is_down
from periscope_community.probes import Probe
from periscope_community.pve import GuestState, PveView
from periscope_community.pve import parse_resources as parse_pve
from periscope_community.timeparse import human_span, parse_duration, parse_when

TZ = ZoneInfo("America/Phoenix")
NOW = dt.datetime(2026, 10, 29, 14, 0, tzinfo=TZ)   # a Thursday, 2pm

RES = ("Plex | plex | http://10.10.10.39:32400 | ct:139 | Media storage; "
       "Media storage | tcp | 10.10.10.140:2049 | vm:137; "
       "Requests | http | http://10.10.10.62:5055 | ct:119; "
       "Twingate | guest | | ct:105; "
       "Minecraft: Horror | minecraft | 10.200.0.229:25565 | ct:127")


# ----- time --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,secs", [("2h", 7200), ("90m", 5400), ("1h30m", 5400), ("1.5h", 5400), ("45", 2700),
                                       ("1 day", 86400), ("2 hours", 7200)])
def test_durations(text, secs):
    assert parse_duration(text) == secs


@pytest.mark.parametrize("text", ["", "soon", "3 weeks", "0m"])
def test_bad_durations(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def at(text):
    return dt.datetime.fromtimestamp(parse_when(text, TZ, NOW), TZ)


def test_when():
    assert at("now") == NOW
    assert at("in 2h") == NOW + dt.timedelta(hours=2)
    assert at("10pm") == NOW.replace(hour=22)
    assert at("1pm") == (NOW + dt.timedelta(days=1)).replace(hour=13)          # already past today → tomorrow
    assert at("tomorrow 9am") == (NOW + dt.timedelta(days=1)).replace(hour=9)
    assert at("tonight 10:30pm") == NOW.replace(hour=22, minute=30)
    assert at("fri 22:00") == (NOW + dt.timedelta(days=1)).replace(hour=22)
    assert at("thursday 1pm") == (NOW + dt.timedelta(days=7)).replace(hour=13)  # thursday already gone by
    assert at("2026-10-30 22:00") == dt.datetime(2026, 10, 30, 22, 0, tzinfo=TZ)
    assert at("10/30 10pm") == dt.datetime(2026, 10, 30, 22, 0, tzinfo=TZ)


@pytest.mark.parametrize("text", ["tonight", "10", "friday", "2026-02-30 10pm", "2026-10-01 10pm", "whenever"])
def test_bad_when(text):
    with pytest.raises(ValueError):
        parse_when(text, TZ, NOW)


def test_human_span():
    assert human_span(7260) == "2h 1m" and human_span(30) == "0m" and human_span(90000) == "1d 1h"


# ----- config ------------------------------------------------------------------------------------------------
def test_resources():
    res, problems = parse_resources(RES + "; Broken | ftp | x; Nope | tcp | nohost; Plex | http | http://x")
    assert [r.name for r in res] == ["Plex", "Media storage", "Requests", "Twingate", "Minecraft: Horror"]
    assert res[0].depends == ["Media storage"] and res[0].vmid == 139 and res[0].guest_kind == "lxc"
    assert res[1].guest_kind == "qemu" and res[4].key == "minecraft-horror"
    assert len(problems) == 3


# ----- diagnosis -------------------------------------------------------------------------------------------
def view(**status):
    v = PveView(nodes={"pve3": status.pop("node", True)})
    for vmid, kind in ((139, "lxc"), (137, "qemu"), (119, "lxc"), (105, "lxc"), (127, "lxc")):
        v.guests[vmid] = GuestState(vmid, kind, f"g{vmid}", "pve3", status.get(f"g{vmid}", "running"))
    return v


def run(pve, failing):
    res, _ = parse_resources(RES)
    probes = {r.key: Probe(False if r.name in failing else (None if r.check == "guest" else True), None,
                           "connection refused" if r.name in failing else "") for r in res}
    down = {r.key for r in res if is_down(r, probes[r.key], pve)}
    return group(res, down, pve, probes)


def test_node_down_is_one_cause():
    causes = run(view(node=False), {"Plex", "Media storage", "Requests", "Minecraft: Horror"})
    assert len(causes) == 1 and causes[0].key == "node:pve3"
    assert set(causes[0].affected) == {"Plex", "Media storage", "Requests", "Twingate", "Minecraft: Horror"}


def test_storage_takes_plex_with_it():
    causes = run(view(g137="stopped"), {"Plex", "Media storage"})
    assert len(causes) == 1 and causes[0].key == "guest:137" and causes[0].affected == ["Plex", "Media storage"]


def test_app_not_answering_vs_stopped():
    causes = {c.key: c for c in run(view(g119="stopped"), {"Plex", "Requests"})}
    assert causes["app:plex"].title == "Plex isn't responding" and "connection refused" in causes["app:plex"].why
    assert causes["guest:119"].title == "Requests is stopped"


def test_guest_only_check():
    causes = run(view(g105="stopped"), set())
    assert [c.key for c in causes] == ["guest:105"]
    assert run(PveView(error="down"), set()) == []          # can't see Proxmox → don't call Twingate down


def test_pve_unreachable_everything_down():
    causes = run(PveView(error="x"), {"Plex", "Media storage", "Requests", "Minecraft: Horror"})
    assert len(causes) == 1 and causes[0].key == "pve:unreachable"


def test_pve_parse():
    v = parse_pve([{"type": "node", "node": "pve3", "status": "online"},
                   {"type": "lxc", "vmid": 139, "name": "plex", "node": "pve3", "status": "running"},
                   {"type": "qemu", "vmid": 900, "template": 1}])
    assert v.node_online("pve3") and v.guest(139).status == "running" and v.guest(900) is None


# ----- maintenance book --------------------------------------------------------------------------------------
class FakeState(dict):
    def set(self, k, v):
        self[k] = v


class FakeWindows:
    def __init__(self):
        self.w = {}

    def add(self, **kw):
        self.w[kw["wid"]] = kw

    def remove(self, wid):
        return self.w.pop(wid, None) is not None


def test_book_lifecycle(monkeypatch):
    import periscope_community.book as book_mod
    monkeypatch.setattr(book_mod.time, "time", lambda: NOW.timestamp())   # extend() bases on now; pin it to NOW
    st, win = FakeState(), FakeWindows()
    book = MaintBook(st, win, TZ, slugify=lambda s: s.lower().replace(" ", "-"))
    t0 = NOW.timestamp() + 3600
    e = book.create(start=t0, end=t0 + 7200, reason="Moving Plex", affects=["Plex"], by="chronus")
    assert e["id"] == "m0001" and win.w["psa-m0001"]["keys"] == ["community:plex*"]
    assert win.w["psa-m0001"]["start"].startswith("2026-10-29T15:00") and win.w["psa-m0001"]["start"].endswith("-07:00")
    with pytest.raises(ValueError):
        book.create(start=t0 + 600, end=t0 + 900, reason="x", affects=[], by="y")       # overlaps
    assert book.covering("Plex", t0 + 10)["id"] == "m0001" and book.covering("Requests", t0 + 10) is None
    e = book.extend(book.set_phase(e, "live"), 1800)
    assert e["end"] == t0 + 9000
    book.set_phase(e, "cancelled")
    assert "psa-m0001" not in win.w and book.open() == []


# ----- messages ----------------------------------------------------------------------------------------------
def test_embeds_build():
    from periscope_community.diagnose import Cause
    c = Cause("node:pve3", "pve3 is offline", "why", ["Plex", "Requests"])
    assert "Plex and Requests" in M.outage_embed(c, NOW.timestamp(), lab="ztechnus.com").fields[0].value
    assert "back up" in M.back_line(c, 0, 600)
    entry = {"id": "m0001", "start": NOW.timestamp(), "end": NOW.timestamp() + 3600, "reason": "Moving Plex",
             "affects": [], "phase": "scheduled"}
    assert "everything" in M.maint_embed(entry, lab="x").description
    long = M.psa_embeds("Title", ("para " * 300 + "\n\n") * 6, lab="x")
    assert len(long) >= 2 and all(len(e.description or "") <= 4096 for e in long)
    assert M.split_markdown("# Hello\n\nbody") == ("Hello", "body")
    board = M.board_embed([{"name": "Plex", "state": "down", "since": 1, "why": "pve3 is offline"},
                           {"name": "Requests", "state": "up", "ms": 4, "detail": ""}], [], lab="x", problems=[])
    assert "1 of 2 down" in board.title


# ----- roles ------------------------------------------------------------------------------------------------
def test_ping_roles():
    from types import SimpleNamespace

    from periscope_community import roles
    res, _ = parse_resources("Plex | plex | http://x:1 | ct:1 | | plex members, 123456789012345678; "
                             "MC | tcp | h:1 | | | @horror.mc; Bare | tcp | h:2")
    assert res[0].ping == ["plex members", "123456789012345678"] and res[1].ping == ["horror.mc"] and res[2].ping == []
    guild = SimpleNamespace(roles=[SimpleNamespace(name="Plex Members", id=111111111111111111),
                                   SimpleNamespace(name="horror.mc", id=222222222222222222)])
    bot = SimpleNamespace(guild_id=1, get_guild=lambda gid: guild)
    content, allowed = roles.mention(bot, roles.tokens_for(res, ["Plex"]))
    assert content == "<@&111111111111111111> <@&123456789012345678>"
    assert roles.tokens_for(res, []) == ["plex members", "123456789012345678", "horror.mc"]
    assert roles.mention(bot, roles.tokens_for(res, ["Bare"]), fallback=999999999999999999)[0] == "<@&999999999999999999>"
    assert roles.mention(bot, ["nope"])[0] is None


# ----- linking -----------------------------------------------------------------------------------------------
from periscope_community.config import parse_bridges  # noqa: E402
from periscope_community.linking import (  # noqa: E402
    LinkBook,
    chat_line,
    find_code,
    first_word,
    parse_users,
    short,
    speaker,
)


@pytest.mark.parametrize("text,code", [("link K7Q2MX", "K7Q2MX"), ("**Steve**: link k7q2mx", "K7Q2MX"),
                                       ("<Steve> LINK: K7Q2MX please", "K7Q2MX"), ("relink K7Q2MX", None),
                                       ("link K7Q2MX9", None), ("hello", None)])
def test_find_code(text, code):
    assert find_code(text) == code


@pytest.mark.parametrize("text,who", [("<Steve> hi", "Steve"), ("**Steve**: hi", "Steve"), ("Steve » hi", "Steve"),
                                      ("[Steve_99] link X", "Steve_99"), ("Steve: link K7Q2MX", "Steve"), (r"Steve\_99: hi", "Steve_99"),
                                      ("Server started", ""), ("sonofhades98 left", "")])
def test_speaker(text, who):
    assert speaker(text) == who


def test_speaker_webhook_and_first_word():
    assert speaker("link K7Q2MX", author="Steve", webhook=True) == "Steve"
    assert speaker("x", author="Steve [Horror]", webhook=True) == "Steve"
    assert first_word("sonofhades98 left") == "sonofhades98" and first_word("**chr0nu5** joined") == "chr0nu5"
    assert chat_line("Server started") is None and short("Minecraft: Horror") == "Horror" and short("Plex") == "Plex"


def test_bridges():
    res, _ = parse_resources("Minecraft: Horror | minecraft | h:1 | | | horror players; Plex | plex | http://x:1")
    br, problems = parse_bridges("Minecraft: Horror | 1497466280729645216; Plex | 12 | ; Nope | 5; "
                                 "minecraft-horror | #1497466280729645216 | extra", res)
    assert br[0].roles == ["horror players"] and br[0].channel_id == 1497466280729645216
    assert br[1].roles == ["extra"] and len(problems) == 2          # Plex has no role to give · Nope unknown


def test_link_book():
    st = FakeState()
    book = LinkBook(st)
    code = book.issue(1, now=1000)
    assert len(code) == 6 and book.redeem(code.lower(), now=1001) == 1 and book.redeem(code, now=1002) is None
    old = book.issue(2, now=1000)
    new = book.issue(2, now=1001)
    assert book.redeem(old, now=1002) is None and book.redeem(new, now=1000 + 15 * 60 + 5) is None   # replaced/expired
    assert book.link_mc(1, "Steve", "Minecraft: Horror") is None
    assert book.mc_owner("steve") == 1 and book.add_server(1, "Steve", "Minecraft: Forever")
    assert not book.add_server(1, "Steve", "Minecraft: Forever")
    assert book.servers(1) == ["Minecraft: Horror", "Minecraft: Forever"]
    assert book.link_mc(2, "steve", "Minecraft: Pavion") == 1 and book.mc_owner("Steve") == 2   # name moved
    assert book.get(1) == {} and book.link_plex(2, 42, "stevep") is None and book.plex_owner(42) == 2
    assert [g["name"] for g in book.unlink_mc(2)] == ["steve"] and book.get(2)["plex"]["name"] == "stevep"
    assert book.unlink_plex(2)["id"] == 42 and book.all() == {}


def test_plex_users_xml():
    xml = """<MediaContainer>
      <User id="11" username="alice"><Server machineIdentifier="abc" pending="0"/></User>
      <User id="12" username="bob"><Server machineIdentifier="abc" pending="1"/></User>
      <User id="13" username="carol"><Server machineIdentifier="other" pending="0"/></User>
      <User id="14" title="Kid" home="1"/>
    </MediaContainer>"""
    users = parse_users(xml, "abc")
    assert set(users) == {11, 12, 14} and users[12]["pending"] and not users[11]["pending"]
    assert set(parse_users(xml)) == {11, 12, 13, 14}


def test_announcement_ping():
    from types import SimpleNamespace

    from periscope_community import roles
    guild = SimpleNamespace(roles=[SimpleNamespace(name="plex members", id=111111111111111111)])
    bot = SimpleNamespace(guild_id=1, get_guild=lambda gid: guild)
    content, allowed = roles.announcement(bot, "everyone")
    assert content == "@everyone" and allowed.everyone
    assert roles.announcement(bot, "")[0] == "@everyone" and roles.announcement(bot, "@here")[0] == "@here"
    assert roles.announcement(bot, "none")[0] is None and roles.announcement(bot, "off")[0] is None
    assert roles.announcement(bot, "plex members")[0] == "<@&111111111111111111>"


# ----- pages -------------------------------------------------------------------------------------------------
from periscope_community import pages as P  # noqa: E402

PAGE = """---
channel: 1566514202431193169
color: #5A189A
---
# Welcome
Hello there.

## not a new card
still the welcome card

# Rules
Be decent.
"""


def test_page_parse():
    p = P.parse("welcome", PAGE)
    assert p.channel_id == 1566514202431193169 and p.color == 0x5A189A
    assert [t for t, _ in p.sections] == ["Welcome", "Rules"]
    assert "## not a new card" in p.sections[0][1]
    assert p.digest == P.parse("welcome", PAGE).digest != P.parse("welcome", PAGE + "x").digest


@pytest.mark.parametrize("text", ["# no front matter", "---\nchannel: nope\n---\n# x", "---\nchannel: 1\n---\n",
                                  "---\nchannel: 1\ncolor: purple\n---\n# x"])
def test_page_bad(text):
    with pytest.raises(ValueError):
        P.parse("bad", text)


def test_page_intro_and_batches():
    p = P.parse("faq", "---\nchannel: <#42>\n---\nintro line\n" + "".join(f"# Q{i}\n{'a' * 900}\n" for i in range(12)))
    assert p.channel_id == 42 and p.sections[0] == ("", "intro line")
    msgs = P.embeds(p, lab="ztechnus.com")
    assert sum(len(m) for m in msgs) == 13 and all(len(m) <= 10 for m in msgs)
    assert all(sum(len(e.title or "") + len(e.description or "") for e in m) <= 6000 for m in msgs)
    assert msgs[0][0].title is None and msgs[-1][-1].footer.text and not msgs[0][1].footer.text


class _Msg:
    _n = 100

    def __init__(self, ch):
        _Msg._n += 1
        self.id, self.ch, self.embeds = _Msg._n, ch, None

    async def edit(self, content=None, embeds=None):
        self.embeds = embeds
        self.ch.edits += 1

    async def delete(self):
        self.ch.msgs.pop(self.id, None)


class _Chan:
    name = "welcome"

    def __init__(self):
        self.msgs, self.sent, self.edits = {}, 0, 0

    async def send(self, embeds=None, allowed_mentions=None):
        m = _Msg(self)
        m.embeds = embeds
        self.msgs[m.id] = m
        self.sent += 1
        return m

    async def fetch_message(self, mid):
        import discord
        if mid not in self.msgs:
            raise discord.NotFound(type("R", (), {"status": 404, "reason": "gone"})(), "gone")
        return self.msgs[mid]


class _State(dict):
    def set(self, k, v):
        self[k] = v


@pytest.mark.asyncio
async def test_page_sync_posts_then_edits(tmp_path):
    ch = _Chan()
    bot = type("B", (), {"lab_name": "ztechnus.com"})()

    async def get_channel_safe(cid):
        return ch
    bot.get_channel_safe = get_channel_safe
    st = _State()
    (tmp_path / "welcome.md").write_text(PAGE, encoding="utf-8")
    await P.sync(bot, tmp_path, st)
    assert ch.sent == 1 and ch.edits == 0
    await P.sync(bot, tmp_path, st)                       # unchanged: nothing happens
    assert ch.sent == 1 and ch.edits == 0
    (tmp_path / "welcome.md").write_text(PAGE.replace("Be decent.", "Be very decent."), encoding="utf-8")
    await P.sync(bot, tmp_path, st)                       # changed: edited in place, not reposted
    assert ch.sent == 1 and ch.edits == 1
    assert st["pages"]["welcome"]["mids"] == list(ch.msgs)
