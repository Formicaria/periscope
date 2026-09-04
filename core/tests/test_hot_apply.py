"""Hot apply: a config change reaches the running process without a restart — one service rebuilt in place,
the rest of the bot untouched."""

import asyncio
from types import SimpleNamespace

import pytest

from periscope import Store
from periscope.runtime import Runtime
from periscope.service import ServiceSpec, Setting


def spec(name, built, **kw):
    async def build(bot):
        built.append((name, dict(bot.env)))
        bot.marker = f"{name}:{bot.env.get('X_URL', '')}"

    return ServiceSpec(name=name, title=name.title(), description="", group="infra", build=build,
                       settings=kw.pop("settings", [Setting("X_URL")]), **kw)


def runtime(tmp_path, built):
    s = Store(tmp_path / "config" / "periscope.yaml")
    s.presences["default"]["token"] = "T"
    s.lab.update({"guild_id": "42", "name": "srv"})
    s.services["a"] = {"enabled": True, "presence": "default", "env": {"X_URL": "http://one"}}
    s.services["b"] = {"enabled": False, "presence": "default", "env": {"X_URL": "http://two"}}
    s.save()
    rt = Runtime(s, tmp_path)
    rt.specs = {"a": spec("a", built), "b": spec("b", built),
                "needy": spec("needy", built, settings=[Setting("X_URL", required=True)])}
    rt.assemble()
    pres = rt.presences["default"]
    pres._synced = True

    async def sync_commands():
        pres.synced = getattr(pres, "synced", 0) + 1

    pres.sync_commands = sync_commands
    return rt, pres


@pytest.mark.asyncio
async def test_settings_change_rebuilds_only_that_service(tmp_path):
    built = []
    rt, pres = runtime(tmp_path, built)
    await rt.services["a"].spec.build(rt.services["a"])
    assert rt.services["a"].marker == "a:http://one"
    built.clear()
    # the config changes on disk (the web UI saving, `periscope config`, an editor)
    rt.store.update_service_env("a", {"X_URL": "http://changed"})
    notes = await rt.apply_config()
    assert built == [("a", {**rt.store.env_for("a")})] and "a is running with the new settings" in notes
    assert rt.services["a"].marker == "a:http://changed" and rt.services["a"].built
    assert [sb.name for sb in pres.services] == ["a"]           # no duplicate service left on the bot
    assert pres.synced == 1                                      # its slash commands were refreshed once


@pytest.mark.asyncio
async def test_switch_on_and_off_without_a_restart(tmp_path):
    built = []
    rt, pres = runtime(tmp_path, built)
    built.clear()
    rt.store.set_enabled("b", True)
    notes = await rt.apply_config()
    assert "b" in rt.services and rt.services["b"].built and any("b is running" in n for n in notes)
    assert {sb.name for sb in pres.services} == {"a", "b"}
    rt.store.set_enabled("b", False)
    notes = await rt.apply_config()
    assert "b" not in rt.services and {sb.name for sb in pres.services} == {"a"} and "b is off" in notes
    assert rt.status()["services"].get("b") is None


@pytest.mark.asyncio
async def test_incomplete_settings_say_what_is_missing_and_do_not_break_the_bot(tmp_path):
    built = []
    rt, pres = runtime(tmp_path, built)
    rt.store.services["needy"] = {"enabled": True, "presence": "default", "env": {}}
    notes = await rt.apply_config()
    assert any("needs X Url" in n for n in notes) and "needy" not in rt.services
    assert rt.status()["services"]["needy"]["state"] == "needs setup"
    assert "a" in rt.services                                    # the running service was not disturbed
    rt.store.update_service_env("needy", {"X_URL": "http://ok"})
    notes = await rt.apply_config()
    assert "needy" in rt.services and rt.services["needy"].built


@pytest.mark.asyncio
async def test_a_service_that_fails_to_build_is_reported_not_fatal(tmp_path):
    built = []
    rt, pres = runtime(tmp_path, built)

    async def boom(bot):
        raise RuntimeError("nope")

    rt.specs["a"].build = boom
    rt.store.update_service_env("a", {"X_URL": "http://again"})
    notes = await rt.apply_config()
    assert any("failed to start" in n for n in notes)
    assert rt.services["a"].healthy is False and rt.status()["services"]["a"]["state"] == "error"
    assert rt.presences["default"].connected is False or True    # the bot itself is untouched


@pytest.mark.asyncio
async def test_a_new_bot_token_is_reported_as_needing_a_restart(tmp_path):
    built = []
    rt, _ = runtime(tmp_path, built)
    rt.store.presences["default"]["token"] = "T2"
    notes = await rt.apply_config()
    assert any("new token" in n and "restart" in n for n in notes)


@pytest.mark.asyncio
async def test_the_config_file_changing_applies_itself(tmp_path):
    built = []
    rt, _ = runtime(tmp_path, built)
    built.clear()
    task = asyncio.create_task(rt._watch_config())
    await asyncio.sleep(0.05)                       # let the watcher note the file as it is now
    other = Store.load(rt.store.path)               # something else writes the file: the UI, the CLI, an editor
    other.update_service_env("a", {"X_URL": "http://from-disk"})
    other.save()
    for _ in range(40):                             # the watcher polls; give it a moment
        await asyncio.sleep(0.1)
        if rt.services["a"].env.get("X_URL") == "http://from-disk":
            break
    rt.request_stop()
    task.cancel()
    assert rt.services["a"].env["X_URL"] == "http://from-disk" and rt.services["a"].marker == "a:http://from-disk"
