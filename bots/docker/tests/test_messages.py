"""The board kind: it previews from its sample, and a customisation is what actually gets posted."""

import json
from types import SimpleNamespace

from periscope import JsonState, StatusBoard
from periscope.messages import REGISTRY, STANDARD_VARIABLES, Messages, MessageStore, kinds_for, preview

from periscope_docker import service  # noqa: F401  — importing the service module is what registers the kinds
from periscope_docker.cogs.status import BOARD_KIND
from periscope_docker.messages import LAB


def _parts(embed):
    """What a template reproduces of an embed."""
    return (embed.title, embed.description, embed.url, embed.color.value if embed.color else None,
            [(f.name, f.value, f.inline) for f in embed.fields], embed.footer.text if embed.footer else None)


def test_the_board_is_registered_and_previews_from_its_sample():
    kinds = kinds_for("docker")
    assert {k.key for k in kinds} == {BOARD_KIND}          # everything else goes out as the core alert kind
    kind = REGISTRY[BOARD_KIND]
    assert kind.title and kind.description and kind.where and kind.where_env == "STATUS_CHANNEL_ID"
    assert kind.group == "boards" and kind.sample is not None

    embed, ctx = kind.sample()
    assert embed.title == "🔴 Docker — /var/run/docker.sock" and embed.footer.text == f"🧪 {LAB}"
    assert embed.description.startswith("**3/6** containers running · 1 unhealthy · 1 restarting · 2 stopped")
    assert [f.name for f in embed.fields] == ["Containers (6)", "⬆ Images with updates (2)"]
    json.dumps(ctx)                                        # plain values only
    assert not set(ctx) & set(STANDARD_VARIABLES)          # never shadows the embed's own parts
    assert set(kind.variables) == set(ctx)                 # what is documented is what is passed
    again, ctx_again = kind.sample()
    assert _parts(again) == _parts(embed) and ctx_again == ctx        # deterministic

    rendered, full, err = preview(kind, None)
    assert err is None and _parts(rendered) == _parts(embed)          # the default template reproduces the board
    assert full["lab"] == "lab" and full["service"] == "docker" and full["title"] == embed.title


def test_the_facts_a_customised_board_can_use(tmp_path):
    embed, data = REGISTRY[BOARD_KIND].sample()
    assert data["version"] == "27.1.1" and data["endpoint"] == "/var/run/docker.sock"
    assert data["counts"]["running"] == 3 and data["down"] == ["radarr", "immich-ml", "pgbackup"]
    assert data["checking_updates"] is True and data["updates"] == ["linuxserver/sonarr:4.0.9", "traefik:v3.1"]
    rows = {c["name"]: c for c in data["containers"]}
    assert rows["jellyfin"]["cpu"] == 24.0 and rows["jellyfin"]["mem"] == 1_207_959_552
    assert rows["jellyfin"]["trouble"] == "" and rows["jellyfin"]["uptime_s"] == 12 * 86400
    assert rows["radarr"]["trouble"] == "crashed" and rows["radarr"]["exit_code"] == 137
    assert rows["sonarr"]["health"] == "unhealthy" and rows["radarr"]["cpu"] is None

    store = MessageStore(tmp_path / "config" / "messages.yaml")
    bot = SimpleNamespace(state=JsonState(tmp_path / "state.json"), name="docker",
                          settings=SimpleNamespace(status_channel_id=1),
                          messages=Messages(store, service="docker", lab="THE LAB"))
    board = StatusBoard(bot, key="docker", kind=BOARD_KIND)
    assert _parts(board.customise(embed, data)) == _parts(embed)      # no customisation: untouched

    store.set(BOARD_KIND, {"title": "🐳 {{ lab }} — {{ counts.running }}/{{ counts.total }} up",
                           "description": "{{ down | join(', ') }} · docker {{ version }}", "color": "auto",
                           "fields": [{"repeat": "updates", "name": "{{ item }}", "value": "update ready",
                                       "inline": True}],
                           "footer": "{{ endpoint }}", "timestamp": True})
    custom = board.customise(embed, data)
    assert custom.title == "🐳 THE LAB — 3/6 up" and custom.description == "radarr, immich-ml, pgbackup · docker 27.1.1"
    assert [(f.name, f.value) for f in custom.fields] == [("linuxserver/sonarr:4.0.9", "update ready"),
                                                          ("traefik:v3.1", "update ready")]
    assert custom.footer.text == "/var/run/docker.sock" and custom.color.value == embed.color.value

    store.set(BOARD_KIND, None, enabled=False)
    assert board.customise(embed, data) is None                       # switched off: the board comes down
