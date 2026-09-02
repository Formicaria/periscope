import os

from periscope import JsonState, Severity, env_bool, env_list, human_bytes, human_duration, progress_bar, lab_embed


def test_helpers():
    assert human_bytes(1536) == "1.5 KB"
    assert human_duration(3661) == "1h 1m"
    assert progress_bar(50, 10).startswith("█████░░░░░")


def test_env_parsing(monkeypatch):
    monkeypatch.setenv("X_BOOL", "yes")
    monkeypatch.setenv("X_LIST", "a, b,,c")
    assert env_bool("X_BOOL") is True
    assert env_list("X_LIST") == ["a", "b", "c"]


def test_state_roundtrip(tmp_path):
    s = JsonState(tmp_path / "state.json")
    s.set("k", {"a": 1})
    s2 = JsonState(tmp_path / "state.json")
    assert s2.get("k") == {"a": 1}
    ns = s2.namespace("alerts")
    ns.set("fp", 1)
    assert s2.get("alerts:fp") == 1


def test_embed():
    e = lab_embed("Title", "desc", severity=Severity.CRITICAL, lab_name="lab1")
    assert e.color.value == Severity.CRITICAL.color
    assert "lab1" in e.footer.text


def test_env_strips_inline_comment(monkeypatch):
    from periscope.config import Settings
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("LAB_COLOR", "5A189A  # Hex color used for INFO embeds")
    monkeypatch.setenv("GUILD_ID", "42  # server id")
    monkeypatch.setenv("ADMIN_ROLE_IDS", "1,2  # Comma-separated role ids")
    s = Settings.from_env()
    assert s.lab_color == 0x5A189A and s.guild_id == 42 and s.admin_role_ids == [1, 2]
    from periscope.config import env_bool
    monkeypatch.setenv("FLAG", "true  # note")
    assert env_bool("FLAG") is True
    from periscope.config import env
    monkeypatch.setenv("LIDARR_URL", "# e.g. http://lidarr:8686")   # empty value + trimmed inline comment
    assert env("LIDARR_URL") is None
