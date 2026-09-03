"""The slice of Discord's Markdown an embed preview needs, as safe HTML.

Text is escaped first and only then formatted, so whatever a template renders is *drawn* in the preview and never
executed — which is why the mention patterns below read `&lt;#123&gt;`: by the time they run, the angle brackets
are already escaped. Ids cannot be resolved here, so a mention becomes a plain `#channel` / `@user` / `@role`
pill with the id in its tooltip. Registered as the `md` filter in render.py.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

BLOCK = re.compile(r"```[a-zA-Z0-9+#.-]*\n?(.*?)```", re.S)
INLINE = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
STRIKE = re.compile(r"~~(.+?)~~", re.S)
STAR = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
UNDER = re.compile(r"(?<![\w_])_([^_\n]+)_(?![\w_])")
ROLE = re.compile(r"&lt;@&amp;(\d+)&gt;")
USER = re.compile(r"&lt;@!?(\d+)&gt;")
CHANNEL = re.compile(r"&lt;#(\d+)&gt;")
EMOJI = re.compile(r"&lt;a?:([A-Za-z0-9_]+):\d+&gt;")

MENTION = "rounded px-1 bg-primary/20 text-primary-content/90 whitespace-nowrap"
CODE = "rounded bg-base-300/70 px-1 py-0.5 text-[0.85em]"
PRE = "rounded bg-base-300/70 p-2 my-1 overflow-x-auto text-[0.85em] whitespace-pre-wrap"


def _stash(text: str, pattern: re.Pattern[str], wrap: str, held: list[str]) -> str:
    """Pull code out of the way so bold/italic/mentions cannot run inside it; put a marker in its place."""

    def keep(m: re.Match[str]) -> str:
        held.append(wrap.format(body=m.group(1)))
        return f"\x00{len(held) - 1}\x00"

    return pattern.sub(keep, text)


def discord_markdown(value: object) -> Markup:
    """One embed string (a description, a field value) as the HTML a preview should show."""
    text = str(escape("" if value is None else value))
    held: list[str] = []
    text = _stash(text, BLOCK, f'<pre class="{PRE}"><code>{{body}}</code></pre>', held)
    text = _stash(text, INLINE, f'<code class="{CODE}">{{body}}</code>', held)
    text = LINK.sub(r'<a href="\2" target="_blank" rel="noopener noreferrer" class="link link-primary">\1</a>', text)
    text = BOLD.sub(r"<strong>\1</strong>", text)
    text = STRIKE.sub(r"<s>\1</s>", text)
    text = STAR.sub(r"<em>\1</em>", text)
    text = UNDER.sub(r"<em>\1</em>", text)
    text = ROLE.sub(rf'<span class="{MENTION}" title="role \1">@role</span>', text)
    text = USER.sub(rf'<span class="{MENTION}" title="user \1">@user</span>', text)
    text = CHANNEL.sub(rf'<span class="{MENTION}" title="channel \1">#channel</span>', text)
    text = EMOJI.sub(r":\1:", text)
    text = text.replace("\n", "<br>")
    for i, block in enumerate(held):
        text = text.replace(f"\x00{i}\x00", block)
    return Markup(text)
