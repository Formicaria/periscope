import datetime as dt

from periscope import Severity

from periscope_arr.cogs.media import Stream, parse_jellyfin_session, parse_plex_session, sum_diskspace
from periscope_arr.cogs.queue import (StallTracker, calendar_entries, format_queue_item, group_by_day, queue_item_name,
                                   queue_pct)
from periscope_arr.cogs.webhooks import parse_event

SONARR_GRAB = {
    "eventType": "Grab",
    "series": {"id": 1, "title": "The Expanse", "year": 2015, "tvdbId": 280619,
               "images": [{"coverType": "banner", "remoteUrl": "https://x/banner.jpg"},
                          {"coverType": "poster", "remoteUrl": "https://x/poster.jpg"}]},
    "episodes": [{"seasonNumber": 6, "episodeNumber": 1, "title": "Strange Dogs"}],
    "release": {"quality": "WEBDL-1080p", "releaseGroup": "NTb", "indexer": "NZBgeek", "size": 2147483648},
    "downloadClient": "SABnzbd",
}


def test_parse_grab_event():
    ev = parse_event("sonarr", SONARR_GRAB)
    assert ev.event_type == "Grab"
    assert "The Expanse (2015) – S06E01" in ev.description
    assert ev.poster == "https://x/poster.jpg"
    assert ev.fields["Quality"] == "WEBDL-1080p"
    assert ev.fields["Group"] == "NTb"
    assert ev.fields["Indexer"] == "NZBgeek"
    assert ev.fields["Client"] == "SABnzbd"
    assert ev.fields["Size"] == "2.0 GB"
    assert ev.health_fingerprint is None


def test_parse_radarr_download_nested_quality():
    p = {"eventType": "Download", "movie": {"title": "Dune", "year": 2021, "images": []},
         "movieFile": {"quality": {"quality": {"name": "Bluray-2160p"}}, "releaseGroup": "FLUX"}, "isUpgrade": True}
    ev = parse_event("radarr", p)
    assert ev.severity is Severity.OK
    assert "Dune (2021)" in ev.description
    assert ev.fields["Quality"] == "Bluray-2160p"
    assert ev.fields["Upgrade"] == "yes"
    assert ev.poster is None


def test_health_fingerprint_stable():
    p = {"eventType": "HealthIssue", "level": "error", "type": "IndexerStatusCheck", "message": "All indexers unavailable"}
    a = parse_event("radarr", p)
    b = parse_event("radarr", dict(p, eventType="HealthRestored"))
    assert a.severity is Severity.CRITICAL
    assert a.health_fingerprint == b.health_fingerprint
    assert a.health_fingerprint.startswith("arr:radarr:health:IndexerStatusCheck:")
    assert parse_event("sonarr", p).health_fingerprint != a.health_fingerprint


def test_unknown_event_and_test():
    assert parse_event("prowlarr", {"eventType": "Test"}).title == "Prowlarr: 🔔 Test notification"
    assert parse_event("lidarr", {"eventType": "Weird"}).title == "Lidarr: 📣 Weird"


def test_queue_formatting():
    item = {"id": 7, "title": "Some.Release", "size": 1000, "sizeleft": 250, "status": "downloading",
            "timeleft": "00:05:00", "series": {"title": "Show"}, "episode": {"seasonNumber": 1, "episodeNumber": 2}}
    assert queue_pct(item) == 75.0
    assert queue_item_name("sonarr", item) == "Show S01E02"
    line = format_queue_item("sonarr", item)
    assert "**Show S01E02** `#7`" in line and "75.0%" in line and "ETA 00:05:00" in line
    assert queue_item_name("radarr", {"movie": {"title": "Heat", "year": 1995}}) == "Heat (1995)"
    assert queue_pct({"size": 0, "sizeleft": 0}) == 0.0


def test_calendar_grouping():
    start, end = dt.date(2026, 9, 1), dt.date(2026, 9, 8)
    sonarr = [{"airDateUtc": "2026-09-02T01:00:00Z", "seasonNumber": 2, "episodeNumber": 3, "title": "Ep",
               "series": {"title": "Show"}},
              {"airDateUtc": "2026-10-02T01:00:00Z", "seasonNumber": 2, "episodeNumber": 9, "series": {"title": "Late"}}]
    radarr = [{"title": "Film", "year": 2026, "digitalRelease": "2026-09-02T00:00:00Z", "inCinemas": "2026-05-01T00:00:00Z"}]
    entries = calendar_entries("sonarr", sonarr, start, end) + calendar_entries("radarr", radarr, start, end)
    grouped = group_by_day(entries)
    assert len(grouped) == 1
    day, texts = grouped[0]
    assert day == dt.date(2026, 9, 2)
    assert texts == ["🎬 Film (2026) · digital", "📺 Show S02E03 – Ep"]


def test_stall_tracker():
    t = StallTracker(stall_s=600)
    item = {"id": 1, "sizeleft": 500, "status": "downloading"}
    assert t.update("sonarr", [item], now=0) == ([], [])
    assert t.update("sonarr", [item], now=300) == ([], [])          # not yet stalled
    new, rec = t.update("sonarr", [item], now=700)                   # unchanged ≥ 600s → stalled
    assert [k for k, _ in new] == ["sonarr:1"] and rec == []
    assert t.update("sonarr", [item], now=900) == ([], [])          # already reported, no dupes
    assert t.update("sonarr", [dict(item, sizeleft=400)], now=1000) == ([], ["sonarr:1"])  # progressed → recovered
    t.update("sonarr", [dict(item, sizeleft=400)], now=2000)
    assert t.update("sonarr", [], now=2000)[1] == ["sonarr:1"]      # gone → recovered
    paused = {"id": 2, "sizeleft": 500, "status": "paused"}
    t.update("radarr", [paused], now=0)
    assert t.update("radarr", [paused], now=5000) == ([], [])        # only 'downloading' items count


def test_plex_and_jellyfin_sessions():
    plex = {"type": "episode", "grandparentTitle": "Severance", "parentIndex": 2, "index": 4, "title": "Woe's Hollow",
            "duration": 3000000, "viewOffset": 1500000, "User": {"title": "alice"},
            "Player": {"product": "Plex Web", "state": "playing"},
            "TranscodeSession": {"videoDecision": "copy", "audioDecision": "transcode"}}
    s = parse_plex_session(plex)
    assert s.title == "Severance – S02E04 Woe's Hollow" and s.user == "alice" and s.pct == 50.0
    assert s.method == "transcode" and s.paused is False
    assert "⏸️" not in s.line() and "🔁 transcode" in s.line()

    jf = {"UserName": "bob", "Client": "Jellyfin Media Player",
          "NowPlayingItem": {"Type": "Movie", "Name": "Heat", "ProductionYear": 1995, "RunTimeTicks": 1000},
          "PlayState": {"PositionTicks": 250, "IsPaused": True, "PlayMethod": "DirectPlay"}}
    j = parse_jellyfin_session(jf)
    assert isinstance(j, Stream) and j.title == "Heat (1995)" and j.pct == 25.0 and j.method == "direct" and j.paused
    assert parse_jellyfin_session({"UserName": "idle"}) is None


def test_sum_diskspace_dedupes_paths():
    free, total = sum_diskspace([{"path": "/tv", "freeSpace": 10, "totalSpace": 100},
                                 {"path": "/tv", "freeSpace": 10, "totalSpace": 100},
                                 {"path": "/movies", "freeSpace": 5, "totalSpace": 50}])
    assert (free, total) == (15, 150)
