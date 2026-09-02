import pytest

from periscope_arr.config import ArrSettings

ALL_VARS = ["SONARR_URL", "SONARR_API_KEY", "RADARR_URL", "RADARR_API_KEY", "LIDARR_URL", "LIDARR_API_KEY",
            "PROWLARR_URL", "PROWLARR_API_KEY", "QBIT_URL", "QBIT_USER", "QBIT_PASS", "SABNZBD_URL",
            "SABNZBD_API_KEY", "PLEX_URL", "PLEX_TOKEN", "JELLYFIN_URL", "JELLYFIN_API_KEY", "VERIFY_SSL",
            "MEDIA_CHANNEL_ID", "ARR_QUEUE_STALL_MIN"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in ALL_VARS:
        monkeypatch.delenv(v, raising=False)


def test_parses_enabled_services(monkeypatch):
    monkeypatch.setenv("SONARR_URL", "http://sonarr:8989/")
    monkeypatch.setenv("SONARR_API_KEY", "abc")
    monkeypatch.setenv("QBIT_URL", "qbit:8080")
    monkeypatch.setenv("PLEX_URL", "https://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "tok")
    monkeypatch.setenv("VERIFY_SSL", "false")
    monkeypatch.setenv("MEDIA_CHANNEL_ID", "123")
    monkeypatch.setenv("ARR_QUEUE_STALL_MIN", "45")
    cfg = ArrSettings.from_env()
    assert cfg.arr == {"sonarr": ("http://sonarr:8989", "abc")}
    assert cfg.qbit_url == "http://qbit:8080"
    assert cfg.plex_url == "https://plex:32400"
    assert cfg.verify_ssl is False
    assert cfg.media_channel_id == 123
    assert cfg.queue_stall_min == 45
    assert cfg.enabled_services() == ["sonarr", "qbittorrent", "plex"]


def test_url_without_key_fails(monkeypatch):
    monkeypatch.setenv("RADARR_URL", "http://radarr:7878")
    with pytest.raises(RuntimeError, match="RADARR_API_KEY"):
        ArrSettings.from_env()


def test_nothing_configured_fails():
    with pytest.raises(RuntimeError, match="No services configured"):
        ArrSettings.from_env()
