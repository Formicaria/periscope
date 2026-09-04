"""The Messages page: the gallery, both editor tabs, the live preview, on/off + reset, and a test post."""

from __future__ import annotations

import json
from types import SimpleNamespace

from periscope.messages import MessageStore
from periscope_web.markdown import discord_markdown as md

HX = {"HX-Request": "true"}
BUSY, BOARD, GRABBED = "pve.node_busy", "pve.board", "sonarr.grabbed"


def msgs(runtime) -> MessageStore:
    """A fresh reader of config/messages.yaml — proves the customisation really reached the file."""
    return MessageStore(runtime.messages.path)


def simple_form(**over) -> dict:
    form = {"mode": "simple", "title": "{{ title }}", "description": "{{ description }}", "color_mode": "auto",
            "footer": "{{ lab }}", "passthrough": "{}"}
    form.update(over)
    return form


# ----- gallery ---------------------------------------------------------------------------------------------
async def test_gallery_groups_kinds_and_marks_customised_and_off(client, runtime):
    runtime.messages.set(GRABBED, {"title": "Got it", "description": "{{ description }}"})
    runtime.messages.set(BOARD, None, enabled=False)
    r = await client.get("/messages")
    assert r.status_code == 200
    html = r.text
    assert 'id="msg-pve-node_busy"' in html and 'id="msg-pve-board"' in html and 'id="msg-sonarr-grabbed"' in html
    assert "Every service" in html and "Proxmox VE" in html and "Sonarr" in html      # service headings
    assert "Nightly digest" in html and "Infrastructure" in html                      # an umbrella name → its group's label
    assert html.index("Every service") < html.index("Proxmox VE")                     # the shared alerts come first
    assert ">alerts<" in html and ">boards<" in html and ">feed<" in html             # the kinds' own groups
    assert "Node busy" in html and "posted when a node stays over its CPU threshold" in html
    assert "goes to the alert channel" in html
    assert "reworded" in html and "switched off" in html
    assert "pve1 is busy" in html and "<strong>93%</strong>" in html                   # the sample, drawn as Discord draws it
    assert "#F1C40F" in html                                                          # its colour bar
    assert "Got it" in html                                                           # the customised wording, not the default
    assert 'id="msg-search"' in html
    assert 'href="/messages"' in html                                                 # in the nav


# ----- editor ----------------------------------------------------------------------------------------------
async def test_editor_renders_both_tabs_and_the_variable_chips(client):
    r = await client.get(f"/messages/{BUSY}")
    assert r.status_code == 200
    html = r.text
    assert 'data-tab="simple"' in html and 'data-tab="code"' in html
    assert 'data-pane="simple"' in html and 'data-pane="code"' in html
    assert 'name="title"' in html and 'name="description"' in html and 'name="color_mode"' in html and 'name="footer"' in html
    assert 'name="keep_fields"' in html and 'name="field_name"' in html and 'name="field_value"' in html
    assert 'data-var="{{ node }}"' in html and "the node&#39;s name" in html           # the kind's own variables
    assert 'data-var="{{ description }}"' in html and 'data-var="{{ lab }}"' in html   # the standard ones
    assert "&#34;repeat&#34;: &#34;fields&#34;" in html                                # the code tab holds the same template
    assert ">Default<" in html and ">Yours<" in html and 'id="preview"' in html
    assert "Send a test post" in html
    r = await client.get("/messages/nope.nope")
    assert r.status_code == 404


async def test_save_from_the_simple_tab_writes_the_template(client, runtime):
    form = simple_form(title="🔥 {{ title }}", color_mode="custom", color_hex="ff0000", keep_fields="true",
                       passthrough=json.dumps({"url": "{{ url }}", "thumbnail": "{{ thumbnail }}", "timestamp": "auto"}))
    form.update({"field_row": ["0", "1", "2"], "field_name": ["Node", "Runbook", ""],
                 "field_value": ["{{ node }}", "https://wiki/pve", ""],      # the empty row is dropped
                 "field_inline_0": "true"})
    r = await client.post(f"/messages/{BUSY}", data=form, headers=HX)
    assert r.status_code == 200 and "applies to the next post" in r.text
    assert msgs(runtime).template_for(BUSY) == {
        "title": "🔥 {{ title }}", "description": "{{ description }}", "url": "{{ url }}", "color": "#FF0000",
        "fields": [{"repeat": "fields", "name": "{{ item.name }}", "value": "{{ item.value }}", "inline": "{{ item.inline }}"},
                   {"name": "Node", "value": "{{ node }}", "inline": True},
                   {"name": "Runbook", "value": "https://wiki/pve", "inline": False}],
        "footer": "{{ lab }}", "thumbnail": "{{ thumbnail }}", "timestamp": "auto",
    }
    assert msgs(runtime).enabled(BUSY) is True
    r = await client.get(f"/messages/{BUSY}")
    assert 'value="Runbook"' in r.text and 'value="#FF0000"' in r.text and "Reset to default" in r.text
    # a colour that is neither a pick nor a severity is refused, with the wording next to the form
    r = await client.post(f"/messages/{BUSY}", data=simple_form(color_mode="custom", color_hex="zzz"), headers=HX)
    assert r.status_code == 422 and "six hex digits" in r.text
    assert msgs(runtime).template_for(BUSY)["color"] == "#FF0000"                    # nothing written


async def test_save_from_the_code_tab(client, runtime):
    good = json.dumps({"title": "{{ title }}", "description": "{{ description }}", "color": "warning"})
    r = await client.post(f"/messages/{BUSY}", data={"mode": "code", "code": good}, headers=HX)
    assert r.status_code == 200
    assert msgs(runtime).template_for(BUSY) == {"title": "{{ title }}", "description": "{{ description }}", "color": "warning"}
    r = await client.post(f"/messages/{BUSY}", data={"mode": "code", "code": "{oops"}, headers=HX)
    assert r.status_code == 422 and "not valid JSON" in r.text and "line 1" in r.text
    assert "{oops" in r.text                                                          # what was typed comes back
    r = await client.post(f"/messages/{BUSY}", data={"mode": "code", "code": json.dumps({"titel": "x"})}, headers=HX)
    assert r.status_code == 422 and "unknown key" in r.text
    r = await client.post(f"/messages/{BUSY}", data={"mode": "code", "code": json.dumps({"title": "x", "color": "zzz"})}, headers=HX)
    assert r.status_code == 422 and "colour" in r.text                                # renders, but not to an embed
    assert msgs(runtime).template_for(BUSY)["color"] == "warning"                     # still the last good one
    r = await client.post(f"/messages/{BUSY}", data={"mode": "code", "code": good})    # non-HTMX
    assert r.status_code == 303 and r.headers["location"] == f"/messages/{BUSY}"
    r = await client.post(f"/messages/{BUSY}", data={"mode": "code", "code": "{oops"})
    assert r.status_code == 422 and "not valid JSON" in r.text and 'data-pane="code"' in r.text


async def test_the_simple_tab_carries_what_it_does_not_show(client, runtime):
    """Editing the wording must not quietly drop the image, the link or the author line."""
    runtime.messages.set(BUSY, {"title": "t", "description": "d", "url": "https://u", "color": "auto", "fields": [],
                               "footer": {"text": "f", "icon_url": "https://i/x.png"}, "image": "https://i",
                               "author": "periscope", "timestamp": True})
    r = await client.get(f"/messages/{BUSY}")
    assert "does more than these boxes can show" in r.text and "an icon next to the footer" in r.text
    keep = json.dumps({"url": "https://u", "image": "https://i", "timestamp": True, "author": "periscope"})
    r = await client.post(f"/messages/{BUSY}", data=simple_form(title="new wording", passthrough=keep), headers=HX)
    assert r.status_code == 200
    saved = msgs(runtime).template_for(BUSY)
    assert saved["title"] == "new wording" and saved["url"] == "https://u" and saved["image"] == "https://i"
    assert saved["author"] == "periscope" and saved["timestamp"] is True


# ----- live preview ----------------------------------------------------------------------------------------
async def test_preview_renders_the_unsaved_template_and_boxes_errors(client, runtime):
    form = simple_form(title="Busy: {{ node }}", description="<b>raw</b> **on** {{ cpu }}%")
    r = await client.post(f"/messages/{BUSY}/preview", data=form, headers=HX)
    assert r.status_code == 200
    assert "Busy: pve1" in r.text and "<strong>on</strong> 93%" in r.text
    assert "&lt;b&gt;raw&lt;/b&gt;" in r.text and "<b>raw</b>" not in r.text          # html in a template is drawn, not run
    assert ">Default<" in r.text and ">Yours<" in r.text and "pve1 is busy" in r.text  # the shipped wording next to it
    assert msgs(runtime).template_for(BUSY) is None                                    # a preview saves nothing
    r = await client.post(f"/messages/{BUSY}/preview", data={"mode": "code", "code": "nope"}, headers=HX)
    assert r.status_code == 200 and 'id="preview-error"' in r.text and "not valid JSON" in r.text
    r = await client.post(f"/messages/{BUSY}/preview", data={"mode": "code", "code": json.dumps({"color": "zzz"})}, headers=HX)
    assert 'id="preview-error"' in r.text and "colour" in r.text


# ----- switch off / reset -----------------------------------------------------------------------------------
async def test_toggle_and_reset_from_a_card(client, runtime):
    r = await client.post(f"/messages/{BOARD}/toggle", headers=HX)
    assert r.status_code == 200 and 'id="msg-pve-board"' in r.text and "Switch on" in r.text and "switched off" in r.text
    assert msgs(runtime).enabled(BOARD) is False
    r = await client.post(f"/messages/{BOARD}/toggle", headers=HX)
    assert r.status_code == 200 and "Switch off" in r.text
    store = msgs(runtime)
    assert store.enabled(BOARD) is True and store.customised(BOARD) is False           # the entry is gone again
    runtime.messages.set(BOARD, {"title": "mine"})
    r = await client.post(f"/messages/{BOARD}/reset", headers=HX)
    assert r.status_code == 200 and "Cluster board" in r.text and "mine" not in r.text
    assert msgs(runtime).template_for(BOARD) is None
    r = await client.post("/messages/nope.nope/toggle", headers=HX)
    assert r.status_code == 404


async def test_reset_from_the_editor_goes_back_to_the_page(client, runtime):
    runtime.messages.set(BUSY, {"title": "mine"})
    r = await client.post(f"/messages/{BUSY}/reset", data={"scope": "editor"}, headers=HX)
    assert r.status_code == 200 and r.headers.get("HX-Redirect") == f"/messages/{BUSY}"
    assert msgs(runtime).template_for(BUSY) is None


async def test_a_message_save_never_asks_for_a_restart(client, app):
    await client.post(f"/messages/{BUSY}", data=simple_form(title="new"), headers=HX)
    assert app.state.pending == [] and app.state.dirty() is False
    r = await client.get("/messages")
    assert "restart to apply" not in r.text


# ----- test post -------------------------------------------------------------------------------------------
async def test_test_post_goes_to_the_channel_the_kind_names(client, runtime):
    pres = runtime.presences["default"]
    runtime.services["pve"] = SimpleNamespace(presence=pres)
    r = await client.post(f"/messages/{BOARD}/test", data=simple_form(title="Board: {{ title }}"), headers=HX)
    assert r.status_code == 200 and "posted a test to #lab-status" in r.text           # STATUS_CHANNEL_ID
    assert [e.title for e in pres.channels[1001].sent] == ["Board: Cluster board"]
    r = await client.post(f"/messages/{BUSY}/test", data=simple_form(), headers=HX)    # ALERT_CHANNEL_ID
    assert r.status_code == 200 and "#lab-alerts" in r.text and len(pres.channels[1002].sent) == 1
    # MEDIA_CHANNEL_ID is unset for sonarr → the lab's alert channel
    r = await client.post(f"/messages/{GRABBED}/test", data=simple_form(), headers=HX)
    assert r.status_code == 200 and "#lab-alerts" in r.text and len(pres.channels[1002].sent) == 2


async def test_test_post_for_a_shared_kind_invents_no_service(client, runtime):
    """`core` and the media hub are names, not services — a test post must not conjure one into the config."""
    runtime.services["pve"] = SimpleNamespace(presence=runtime.presences["default"])
    r = await client.post("/messages/core.alert/test", headers=HX)
    assert r.status_code == 200 and "#lab-alerts" in r.text                             # the lab's alert channel
    assert "core" not in runtime.store.services


async def test_test_post_explains_itself_when_it_cannot_post(client, runtime, store):
    r = await client.post(f"/messages/{BUSY}/test", data={"mode": "code", "code": "{oops"}, headers=HX)
    assert r.status_code == 422 and "fix the template first" in r.text
    runtime.presences["default"].connected = False
    r = await client.post(f"/messages/{BUSY}/test", data=simple_form(), headers=HX)
    assert r.status_code == 422 and "no bot is connected" in r.text
    runtime.presences["default"].connected = True
    store.lab["alert_channel_id"] = ""
    store.save()
    r = await client.post(f"/messages/{BUSY}/test", data=simple_form(), headers=HX)
    assert r.status_code == 422 and "no channel to post to yet" in r.text and "ALERT_CHANNEL_ID" in r.text
    store.update_service_env("pve", {"ALERT_CHANNEL_ID": "4004"})                       # a channel the bot cannot see
    store.save()
    r = await client.post(f"/messages/{BUSY}/test", data=simple_form(), headers=HX)
    assert r.status_code == 422 and "cannot see channel 4004" in r.text


# ----- the markdown a preview draws --------------------------------------------------------------------------
def test_markdown_escapes_first_then_formats():
    out = str(md("<img src=x onerror=alert(1)> **bold** *slant* _slant_ ~~gone~~ `code` [docs](https://e/x)"))
    assert "<img" not in out and "&lt;img src=x onerror=alert(1)&gt;" in out
    assert "<strong>bold</strong>" in out and out.count("<em>") == 2 and "<s>gone</s>" in out
    assert ">code</code>" in out and '<a href="https://e/x"' in out and ">docs</a>" in out


def test_markdown_mentions_code_blocks_and_newlines():
    out = str(md("see <#12> <@34> <@&56> <:wave:9>"))
    assert "#channel" in out and "@user" in out and "@role" in out and ":wave:" in out
    assert 'title="channel 12"' in out and 'title="role 56"' in out
    assert str(md("a\nb")).count("<br>") == 1
    assert "<strong>" not in str(md("`**not bold**`"))                                  # code stays literal
    block = str(md("```py\nx = **1**\n```"))
    assert "<pre" in block and "**1**" in block and "<strong>" not in block
    assert str(md(None)) == "" and "&amp;" in str(md("a & b"))
    assert "<a " not in str(md("[x](javascript:alert(1))"))                              # only http(s) links are made
