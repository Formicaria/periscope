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
