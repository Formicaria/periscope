"""v2 core: store round-trip, v1 migration, service assembly, and the ServiceBot facade hosting v1 cogs."""

import asyncio
import json
from pathlib import Path

import pytest

from periscope import Store
from periscope import migrate as migrate_mod
from periscope.migrate import migrate_v1
from periscope.presence import build_intents
from periscope.runtime import Runtime
from periscope.service import Setting, ServiceSpec, settings_from_example


def write_v1(root: Path) -> None:
    (root / "bots" / "proxmox").mkdir(parents=True)
    (root / "bots" / "proxmox" / ".env").write_text(
        "DISCORD_TOKEN=tok-pve\nLAB_NAME=ztechnus.com\nLAB_COLOR=5A189A\nGUILD_ID=42\nSTATUS_CHANNEL_ID=1\nALERT_CHANNEL_ID=2\n"
        "ALERT_ROLE_ID=3\nADMIN_ROLE_IDS=4,5\nWEBHOOK_PORT=8080\nPVE_URL=https://pve:8006\nPVE_TOKEN_ID=periscope@pve!discord\n"
        "PVE_TOKEN_SECRET=s3cret\nPVE_VERIFY_SSL=false\n")
    (root / "bots" / "arr").mkdir(parents=True)
    (root / "bots" / "arr" / ".env").write_text(
        "DISCORD_TOKEN=tok-arr\nLAB_NAME=ztechnus.com\nGUILD_ID=42\nSTATUS_CHANNEL_ID=1\nALERT_CHANNEL_ID=2\nWEBHOOK_PORT=8082\n"
        "WEBHOOK_SECRET=hook\nSONARR_URL=https://sonarr\nSONARR_API_KEY=sk\nRADARR_URL=https://radarr\nRADARR_API_KEY=rk\n"
        "LIDARR_URL=\nQBIT_URL=http://qb:8080\nQBIT_API_KEY=qbt_x\nPLEX_URL=https://plex\nPLEX_TOKEN=pt\nMEDIA_CHANNEL_ID=7\n")
    (root / "bots" / "unifi").mkdir(parents=True)
    (root / "bots" / "unifi" / ".env").write_text("DISCORD_TOKEN=\nUNIFI_URL=https://x\n")  # no token → not imported


def test_store_roundtrip_and_env(tmp_path):
    s = Store(tmp_path / "config" / "periscope.yaml")
    s.lab.update({"name": "lab1", "guild_id": "42", "admin_role_ids": ["4", "5"], "alert_channel_id": "2"})
    s.presences["default"]["token"] = "T"
    s.services["proxmox"] = {"enabled": True, "presence": "default", "env": {"PVE_URL": "u", "ALERT_CHANNEL_ID": "9"}}
    s.save()
    assert oct((tmp_path / "config" / "periscope.yaml").stat().st_mode & 0o777) == "0o600"
    s2 = Store.load(tmp_path / "config" / "periscope.yaml")
    env = s2.env_for("proxmox")
    assert env["DISCORD_TOKEN"] == "T" and env["LAB_NAME"] == "lab1" and env["ADMIN_ROLE_IDS"] == "4,5"
    assert env["ALERT_CHANNEL_ID"] == "9"                       # service override wins over lab default
    assert env["PVE_URL"] == "u" and env["WEBHOOK_PORT"] == "8080"
    red = s2.redacted()
    assert red["presences"]["default"]["token"] == "••••••••"


def test_migrate_v1(tmp_path):
    write_v1(tmp_path)
    (tmp_path / "bots" / "proxmox" / "data").mkdir()
    (tmp_path / "bots" / "proxmox" / "data" / "state.json").write_text(json.dumps({"board:pve:message_id": 5551, "alerts:fp1": {"message_id": 7}}))
    (tmp_path / "bots" / "arr" / "data").mkdir()
    (tmp_path / "bots" / "arr" / "data" / "state.json").write_text(json.dumps({"board:arr:message_id": 5552, "alerts:x": {}}))
    s = Store(tmp_path / "config" / "periscope.yaml")
    created = migrate_v1(s, tmp_path)
    carried = json.loads((tmp_path / "data" / "state.json").read_text())
    assert carried["svc:proxmox:board:pve:message_id"] == 5551 and carried["svc:proxmox:alerts:fp1"] == {"message_id": 7}
    assert carried["presence:arr:board:arr:message_id"] == 5552 and "presence:arr:alerts:x" not in carried
    assert "proxmox" in created and "sonarr" in created and "radarr" in created and "qbittorrent" in created and "plex" in created
    assert "lidarr" not in created and s.services["lidarr"]["enabled"] is False   # present but off
    assert "unifi" not in s.services                                               # no token → not imported
    assert s.lab["name"] == "ztechnus.com" and s.lab["guild_id"] == "42" and s.lab["admin_role_ids"] == ["4", "5"]
    assert s.presences["proxmox"]["token"] == "tok-pve" and s.presences["arr"]["token"] == "tok-arr"
    assert s.services["proxmox"]["presence"] == "proxmox" and s.services["sonarr"]["presence"] == "arr"
    assert s.webhook["secret"] == "hook" and "WEBHOOK_SECRET" not in s.services["sonarr"]["env"]
    env = s.env_for("sonarr")
    assert env["SONARR_API_KEY"] == "sk" and env["MEDIA_CHANNEL_ID"] == "7" and "RADARR_URL" not in env
    assert s.env_for("proxmox")["PVE_TOKEN_SECRET"] == "s3cret"


def test_migrate_v1_standalone_plex_bot(tmp_path, monkeypatch):
    """The standalone Plex bot's .env becomes the `plexrequests` service on its own presence, keys copied verbatim
    plus PLEXREQ_GUILD_ID (its server may differ from the lab's)."""
    write_v1(tmp_path)
    legacy = tmp_path / "legacy-plex"
    legacy.mkdir()
    (legacy / ".env").write_text("DISCORD_TOKEN=tok-plex\nGUILD_ID=77\nCHANNEL_ID=100\nCHANNEL_NAME=join-plex\nREQUESTS_CHANNEL_ID=200\n"
                                 "PLEX_TOKEN=pt\nPLEX_URL=http://plex:32400\nOVERSEERR_URL=\nREQUEST_BACKEND=arr\nRADARR_URL=http://r\n"
                                 "RADARR_API_KEY=rk\nSERVER_NAME=lab.example\nROLE_NAME=plex members\nAUTO_REVOKE=1\n")
    monkeypatch.setattr(migrate_mod, "PLEXREQUESTS_LEGACY_DIR", legacy)
    s = Store(tmp_path / "config" / "periscope.yaml")
    created = migrate_v1(s, tmp_path)
    assert "plexrequests" in created and "proxmox" in created
    assert s.presences["plex"]["token"] == "tok-plex" and s.services["plexrequests"]["presence"] == "plex"
    env = s.services["plexrequests"]["env"]
    assert env["PLEXREQ_GUILD_ID"] == "77" and env["GUILD_ID"] == "77" and "DISCORD_TOKEN" not in env
    assert env["CHANNEL_ID"] == "100" and env["RADARR_API_KEY"] == "rk" and env["AUTO_REVOKE"] == "1" and "OVERSEERR_URL" not in env
    assert s.lab["guild_id"] == "42"                                 # the lab keeps its own server
    flat = s.env_for("plexrequests")
    assert flat["DISCORD_TOKEN"] == "tok-plex" and flat["GUILD_ID"] == "77" and flat["PLEXREQ_GUILD_ID"] == "77"
    # nothing at the legacy location → no plexrequests service
    monkeypatch.setattr(migrate_mod, "PLEXREQUESTS_LEGACY_DIR", tmp_path / "nowhere")
    s2 = Store(tmp_path / "config2" / "periscope.yaml")
    assert "plexrequests" not in migrate_v1(s2, tmp_path) and "plexrequests" not in s2.services


def test_settings_from_example(tmp_path):
    ex = tmp_path / ".env.example"
    ex.write_text("# ---- Proxmox ----\nPVE_URL=https://pve.example:8006  # base url\nPVE_TOKEN_SECRET=\nPVE_VERIFY_SSL=false\n"
                  "PVE_CPU_WARN=85\nDISCORD_TOKEN=\nALERT_CHANNEL_ID=\n")
    st = settings_from_example(ex, required=("PVE_URL",))
    keys = {x.key: x for x in st}
    assert set(keys) == {"PVE_URL", "PVE_TOKEN_SECRET", "PVE_VERIFY_SSL", "PVE_CPU_WARN"}   # shared keys stripped
    assert keys["PVE_URL"].type == "url" and keys["PVE_URL"].required and keys["PVE_URL"].help == "base url"
    assert keys["PVE_TOKEN_SECRET"].type == "secret" and keys["PVE_VERIFY_SSL"].type == "bool" and keys["PVE_CPU_WARN"].type == "int"
    assert keys["PVE_URL"].group == "Proxmox"


def fake_spec(name, *intents, settings=()):
    """A service spec with no package behind it — core tests must not depend on which bots are installed."""

    async def build(bot):
        pass

    return ServiceSpec(name=name, title=name, description="", group="infra", settings=list(settings), build=build,
                       intents=list(intents))


@pytest.mark.asyncio
async def test_runtime_assembles_and_builds_proxmox(tmp_path, monkeypatch):
    """Integration with the real proxmox service package (skipped where only core is installed, e.g. CI's core job)."""
    pytest.importorskip("periscope_proxmox")
    write_v1(tmp_path)
    s = Store(tmp_path / "config" / "periscope.yaml")
    migrate_v1(s, tmp_path)
    s.save()
    for name in list(s.services):
        if name != "proxmox":
            s.services[name]["enabled"] = False
    rt = Runtime(s, tmp_path)
    rt.assemble()
    assert set(rt.services) == {"proxmox"} and set(rt.presences) == {"proxmox"}
    pres = rt.presences["proxmox"]
    sb = rt.services["proxmox"]
    assert sb.settings.alert_channel_id == 2 and sb.settings.admin_role_ids == [4, 5] and sb.lab_name == "ztechnus.com"
    # build the service the way Presence.setup_hook does — no Discord connection needed for that
    await sb.spec.build(sb)
    assert sb.pve_cfg.url == "https://pve:8006" and sb.pve is not None
    assert pres.tree.get_command("pve") is not None
    names = {c.name for c in pres.tree.get_command("pve").commands}
    assert {"nodes", "storage", "tasks"} <= names, names
    assert any(c.qualified_name.startswith("proxmox:") for c in pres.cogs.values())   # namespaced cogs on the presence
    st = rt.status()
    assert st["services"]["proxmox"]["presence"] == "proxmox"
    await sb.pve.close()


@pytest.mark.asyncio
async def test_runtime_skips_broken_config(tmp_path):
    s = Store(tmp_path / "config" / "periscope.yaml")
    s.presences["default"]["token"] = "T"
    s.services["proxmox"] = {"enabled": True, "presence": "default", "env": {"PVE_URL": "https://x"}}   # missing secret
    s.services["nope"] = {"enabled": True, "presence": "default", "env": {}}
    s.services["sonarr"] = {"enabled": True, "presence": "ghost", "env": {}}                          # unknown presence → default token ok
    rt = Runtime(s, tmp_path)
    rt.specs = {"proxmox": fake_spec("proxmox", settings=[Setting("PVE_URL", required=True), Setting("PVE_TOKEN_SECRET", required=True)]),
                "sonarr": fake_spec("sonarr")}
    rt.assemble()
    assert "proxmox" not in rt.services and rt.skipped["proxmox"].startswith("needs ")
    assert "not installed" in rt.skipped["nope"]
    assert "sonarr" in rt.services and rt.services["sonarr"].presence.name == "default"
    st = rt.status()
    assert st["services"]["proxmox"]["state"] == "needs setup" and st["services"]["proxmox"]["fix"] == "settings"


def test_build_intents_unions_named_flags():
    default = build_intents([])
    assert not default.members and not default.message_content and default.guilds
    got = build_intents(["message_content", "members", "members", "no_such_intent"])
    assert got.members and got.message_content and got.guilds
    assert got != default


def test_runtime_unions_service_intents_per_presence(tmp_path):
    """Presences are built with the union of the intents their runnable services declare — before they connect."""
    write_v1(tmp_path)
    s = Store(tmp_path / "config" / "periscope.yaml")
    migrate_v1(s, tmp_path)
    for name in list(s.services):
        s.services[name]["enabled"] = name in ("proxmox", "sonarr")
    s.services["needy"] = {"enabled": True, "presence": "proxmox", "env": {}}                 # shares the proxmox presence
    s.services["skipped"] = {"enabled": True, "presence": "arr", "env": {}}                   # missing a required key
    s.services["quiet"] = {"enabled": True, "presence": "arr", "env": {}}
    rt = Runtime(s, tmp_path)
    rt.specs = {
        "proxmox": fake_spec("proxmox"),
        "sonarr": fake_spec("sonarr"),
        "needy": fake_spec("needy", "members", "message_content"),
        "skipped": fake_spec("skipped", "presences", settings=[Setting("X_URL", required=True)]),
        "quiet": fake_spec("quiet"),
    }
    rt.assemble()
    assert set(rt.services) == {"proxmox", "needy", "sonarr", "quiet"} and rt.skipped["skipped"].startswith("needs ")
    pve, arr = rt.presences["proxmox"], rt.presences["arr"]
    assert pve.intents.members and pve.intents.message_content                      # needy's intents, on its presence
    assert not arr.intents.members and not arr.intents.message_content              # nothing runnable asked for any
    assert not arr.intents.presences                                                # a skipped service contributes nothing
    assert [sb.name for sb in pve.services] == ["proxmox", "needy"]
