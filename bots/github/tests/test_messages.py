"""Message kinds: every github.* card previews from its sample, and the send sites honour a customisation."""

import json
from types import SimpleNamespace

import pytest
from periscope import JsonState
from periscope.messages import REGISTRY, STANDARD_VARIABLES, Messages, MessageStore, kinds_for, preview

from periscope_github import samples, service  # noqa: F401  — importing the service module is what registers the kinds
from periscope_github.config import GithubSettings
from periscope_github.dispatch import Dispatcher
from periscope_github.messages import BOARD_KIND, LAB, TRAIN_KIND, feed_kind
from periscope_github.render import RENDERERS, board_ctx, board_embed, render_event

FEED = {"push", "create", "delete", "pull_request", "pull_request_review", "issues", "issue_comment", "release",
        "workflow_run", "star", "fork", "repository", "member", "organization", "deployment_status", "discussion",
        "discussion_comment", "check_run", "other"}
EXPECTED = {feed_kind(n) for n in FEED} | {BOARD_KIND, TRAIN_KIND}


def _parts(embed):
    """What a template reproduces of an embed (the author line is not part of the identity template)."""
    return (embed.title, embed.description, embed.url, embed.color.value if embed.color else None,
            [(f.name, f.value, f.inline) for f in embed.fields], embed.footer.text if embed.footer else None)


def test_every_card_has_a_kind():
    assert {k.key for k in kinds_for("github")} == EXPECTED
    for k in kinds_for("github"):
        assert k.sample is not None and k.title and k.description and k.where and k.where_env and k.group
    assert set(samples.EVENTS) == FEED
    # every name render_event can hand back (aliases folded, plus the generic fallback) is registered
    names = {render_event(ev, {}, LAB)[0] for ev in RENDERERS} | {"other"}
    assert {feed_kind(n) for n in names} <= EXPECTED


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_sample_previews(key):
    kind = REGISTRY[key]
    embed, ctx = kind.sample()
    assert embed is not None and embed.title and embed.footer.text == f"🧪 {LAB}"
    json.dumps(ctx)                                                   # plain values only
    assert not set(ctx) & set(STANDARD_VARIABLES)                     # never shadows the embed's own parts
    assert set(kind.variables) == set(ctx)                            # what is documented is what is passed
    again, ctx_again = kind.sample()
    assert _parts(again) == _parts(embed) and ctx_again == ctx        # deterministic
    rendered, full, err = preview(kind, None)
    assert err is None and rendered is not None
    assert _parts(rendered) == _parts(embed)                          # the identity template reproduces the card
    assert full["lab"] == "lab" and full["service"] == "github" and full["title"] == embed.title


@pytest.mark.parametrize("name", sorted(FEED))
def test_feed_samples_are_their_own_card(name):
    event, payload = samples.payload(name)
    kind, embed = render_event(event, payload, LAB, verbose=True)
    assert kind == name and embed is not None                         # not the generic fallback in disguise
    if name != "other":
        assert render_event(event, payload, LAB, verbose=False)[1] is not None   # a curated card, not verbose-only


def test_aliases_fold_into_one_kind():
    _, payload = samples.payload("star")
    assert render_event("watch", payload, LAB)[0] == "star"
    _, payload = samples.payload("member")
    assert render_event("membership", payload, LAB)[0] == "member"
    _, payload = samples.payload("other")
    assert render_event("milestone", payload, LAB) == ("other", None)            # nothing without verbose
    # the star card declines an unstar; verbose mode's generic card steps in and is customised as such
    assert render_event("star", {**payload, "action": "deleted"}, LAB, verbose=True)[0] == "other"


def test_board_from_its_facts():
    data = board_ctx("formicaria", [{"name": "a"}], {}, {}, {"a": {"ok": False, "name": "CI", "url": "u"}}, [],
                     poll=False, api_ok=False)
    assert data["open_prs"] is None and data["failing"] == ["a"] and data["source"] == "webhook"
    e = board_embed(data, "THE LAB")
    assert e.title == "🔴 GitHub: formicaria" and "unreachable" in e.description
    assert [(f.name, f.value) for f in e.fields] == [("Repos", "1"), ("Open PRs", "?"), ("Open issues", "?"),
                                                     ("CI (default branch)", "🔴 [a](u) CI"), ("Last events", "—"),
                                                     ("Source", "webhook")]


# ----- a customisation at a send site ------------------------------------------------------------------

class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, **kw):
        self.sent.append(embed)
        return SimpleNamespace(id=len(self.sent), channel=self)


class FakeBot:
    def __init__(self, tmp_path, messages):
        self.state = JsonState(tmp_path / "state.json")
        self.lab_name = "THE LAB"
        self.settings = SimpleNamespace(alert_channel_id=1, status_channel_id=None)
        self.messages = messages
        self.channel = FakeChannel()

    async def get_channel_safe(self, cid):
        return self.channel


@pytest.mark.asyncio
async def test_customised_template_changes_the_post(tmp_path):
    store = MessageStore(tmp_path / "config" / "messages.yaml")
    bot = FakeBot(tmp_path, Messages(store, service="github", lab="THE LAB"))
    d = Dispatcher(bot, GithubSettings())
    event, payload = samples.payload("star")

    assert await d.dispatch(event, payload, delivery_id="s1") is True          # no customisation: the bot's card
    plain = bot.channel.sent[-1]
    assert plain.title == "[anthill] ⭐ starred by bob" and plain.author.name == "bob"

    store.set("github.star", {"title": "🚀 {{ title }}", "description": "{{ description }}", "color": "auto",
                              "fields": [{"name": "Stars", "value": "{{ stars }} in {{ lab }}", "inline": True}],
                              "timestamp": True})
    assert await d.dispatch(event, payload, delivery_id="s2") is True
    custom = bot.channel.sent[-1]
    assert custom.title == "🚀 [anthill] ⭐ starred by bob" and custom.description == "now 42 ⭐"
    assert custom.color.value == plain.color.value and custom.timestamp is not None
    assert [(f.name, f.value, f.inline) for f in custom.fields] == [("Stars", "42 in THE LAB", True)]

    store.set("github.star", None, enabled=False)                            # switched off: nothing goes out
    assert await d.dispatch(event, payload, delivery_id="s3") is False
    assert len(bot.channel.sent) == 2 and d.activity_summary() == {"star": 2}

    store.reset("github.star")                                               # back to the bot's card
    assert await d.dispatch(event, payload, delivery_id="s4") is True
    assert bot.channel.sent[-1].title == plain.title
