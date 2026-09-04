"""The event log and the recap: rows survive the trip to SQLite and back, retention throws away what is old
enough, two writers on one file do not stand on each other, the queries answer what was asked, uptime adds up,
and the digest reads a seeded night correctly."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from periscope.digest import DIGEST_KIND, DigestSchedule, alert_story, build_digest, digest_ctx, digest_kinds
from periscope.history import ALERT_KINDS, History, summarise, window
from periscope.hooks import NullHistory, history_for
from periscope.messages import REGISTRY

HOUR = 3600
DAY = 86400


@pytest.fixture
def log(tmp_path):
    h = History(tmp_path / "data" / "history.db", retention_days=7)
    yield h
    h.close()


def seed(h: History, now: float) -> None:
    """One night: a container that fell over and came back, an alert still open, two media cards, a red build."""
    h.record(service="docker", kind="down", key="sonarr", severity="critical", title="sonarr exited",
             detail="exit code 1", server="testlab", payload={"code": 1}, at=now - 6 * HOUR)
    h.record(service="docker", kind="up", key="sonarr", severity="ok", title="sonarr is running again",
             server="testlab", at=now - 5 * HOUR)
    h.record(service="pve", kind="alert", key="pve1:cpu", severity="warning", title="High CPU on pve1",
             at=now - 4 * HOUR)
    h.record(service="arr", kind="grab", key="sonarr", title="Grabbed: The Expanse S06E01", at=now - 3 * HOUR)
    h.record(service="arr", kind="import", key="sonarr", severity="ok", title="Imported: The Expanse S06E01",
             at=now - 2 * HOUR)
    h.record(service="github", kind="ci", key="anthill", severity="critical",
             title="CI failing: anthill / tests on main", at=now - HOUR)


# ----- the store ---------------------------------------------------------------------------------------
def test_the_file_and_its_tables_are_made_on_first_use(tmp_path):
    path = tmp_path / "nested" / "data" / "history.db"
    h = History(path)
    try:
        h.record(service="pve", kind="up", key="pve1")
        h.flush()
        assert path.exists()
        conn = sqlite3.connect(str(path))
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert {"events", "samples"} <= tables
        assert mode.lower() == "wal"
    finally:
        h.close()


def test_an_event_round_trips_with_every_column_intact(log):
    log.record(service="docker", kind="down", key="sonarr", severity="critical", title="sonarr exited",
               detail="exit code 1", server="testlab", value=1, payload={"image": "sonarr:4", "code": 1})
    (event,) = log.events()
    assert event["service"] == "docker" and event["kind"] == "down" and event["key"] == "sonarr"
    assert event["severity"] == "critical" and event["title"] == "sonarr exited"
    assert event["detail"] == "exit code 1" and event["server"] == "testlab" and event["value"] == 1.0
    assert event["payload"] == {"image": "sonarr:4", "code": 1}
    assert event["ts"] == pytest.approx(time.time(), abs=30)


def test_a_sample_round_trips_as_a_number(log):
    log.sample(service="pve", metric="cpu", value=91.5, key="pve1", server="testlab")
    (point,) = log.series(service="pve", metric="cpu", key="pve1", server="testlab", bucket=DAY)
    assert point[1] == pytest.approx(91.5)
    assert point[0] == pytest.approx(time.time() - time.time() % DAY, abs=DAY)
    assert log.series(service="pve", metric="cpu", key="somewhere-else") == []


def test_an_awkward_payload_is_written_down_or_left_out_never_raised(log):
    log.record(service="pve", kind="note", title="odd", payload={"when": object()})
    log.record(service="pve", kind="note", title="huge", payload={"blob": "x" * 40_000})
    huge, odd = log.events()
    assert huge["payload"] == {}                                 # too big to be worth keeping
    assert str(odd["payload"]["when"]).startswith("<object")     # not JSON, so it is written down as words


def test_writing_never_raises_even_on_nonsense(log):
    log.record(service="pve", kind="up", value="not a number")          # value is simply forgotten
    log.sample(service="pve", metric="cpu", value="not a number")       # the sample is skipped entirely
    assert log.events()[0]["value"] is None
    assert log.series(service="pve", metric="cpu") == []


def test_writing_after_close_is_ignored_and_reading_still_works(tmp_path):
    h = History(tmp_path / "history.db")
    h.record(service="pve", kind="up", key="pve1")
    h.close()
    h.close()                                        # closing twice is not an error
    h.record(service="pve", kind="down", key="pve1")  # dropped, silently
    assert [e["kind"] for e in h.events()] == ["up"]


# ----- retention ---------------------------------------------------------------------------------------
def test_prune_throws_away_what_is_older_than_retention(tmp_path):
    h = History(tmp_path / "history.db", retention_days=0)      # start with retention off, so nothing ages out
    try:
        now = time.time()
        h.record(service="pve", kind="up", key="old", at=now - 10 * DAY)
        h.record(service="pve", kind="up", key="new", at=now - 2 * DAY)
        h.sample(service="pve", metric="cpu", value=1, key="old", at=now - 10 * DAY)
        h.sample(service="pve", metric="cpu", value=2, key="new", at=now - 2 * DAY)
        assert h.prune() == 0 and len(h.events()) == 2
        h.retention_days = 7
        assert h.prune() == 2                        # one event and one sample were past the line
        assert [e["key"] for e in h.events()] == ["new"]
        assert [v for _, v in h.series(service="pve", metric="cpu", bucket=DAY)] == [2.0]
        assert h.prune() == 0                        # a second pass has nothing left to do
    finally:
        h.close()


def test_retention_of_zero_days_keeps_everything(tmp_path):
    h = History(tmp_path / "history.db", retention_days=0)
    try:
        h.record(service="pve", kind="up", key="ancient", at=time.time() - 900 * DAY)
        assert h.prune() == 0
        assert len(h.events()) == 1
    finally:
        h.close()


# ----- concurrency -------------------------------------------------------------------------------------
def test_two_writers_on_one_file_keep_every_row(tmp_path):
    """Two History objects, two connections, two threads each — what two processes look like from SQLite."""
    path = tmp_path / "history.db"
    a, b = History(path), History(path)
    try:
        def write(h: History, tag: str) -> None:
            for i in range(150):
                h.record(service="pve", kind="tick", key=f"{tag}-{i}")
                h.sample(service="pve", metric="cpu", value=float(i), key=tag)

        threads = [threading.Thread(target=write, args=(h, tag))
                   for h, tag in ((a, "a1"), (a, "a2"), (b, "b1"), (b, "b2"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        a.flush()
        b.flush()
        assert a.counts(by="kind") == {"tick": 600}
        assert sum(b.counts(of="samples", by="key").values()) == 600
        assert a.dropped == b.dropped == 0
    finally:
        a.close()
        b.close()


def test_a_second_process_writing_the_same_file_is_read_back_here(tmp_path):
    path = tmp_path / "history.db"
    h = History(path, retention_days=7)
    try:
        h.record(service="pve", kind="up", key="mine")
        h.flush()
        code = ("from periscope.history import History\n"
                f"h = History({str(path)!r})\n"
                "[h.record(service='pve', kind='up', key='theirs') for _ in range(50)]\n"
                "h.close()\n")
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        assert h.counts(by="key") == {"theirs": 50, "mine": 1}
        conn = sqlite3.connect(str(path))             # the file itself is still sound
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    finally:
        h.close()


# ----- queries -----------------------------------------------------------------------------------------
def test_events_filters_on_every_column_it_offers(log):
    now = time.time()
    seed(log, now)
    assert [e["service"] for e in log.events()] == ["github", "arr", "arr", "pve", "docker", "docker"]
    assert [e["kind"] for e in log.events(newest_first=False)][0] == "down"
    assert {e["service"] for e in log.events(service="arr")} == {"arr"}
    assert {e["kind"] for e in log.events(kind=("grab", "import"))} == {"grab", "import"}
    assert [e["title"] for e in log.events(severity="critical", service="github")] == \
        ["CI failing: anthill / tests on main"]
    assert [e["key"] for e in log.events(key="pve1:cpu")] == ["pve1:cpu"]
    assert {e["server"] for e in log.events(server="testlab")} == {"testlab"}
    assert len(log.events(since=now - 3.5 * HOUR)) == 3
    assert len(log.events(until=now - 3.5 * HOUR)) == 3
    assert len(log.events(limit=2)) == 2
    assert [e["kind"] for e in log.events(limit=2, offset=2)] == ["grab", "alert"]
    assert [e["service"] for e in log.events(search="Expanse")] == ["arr", "arr"]
    assert log.events(service="nothing-here") == []


def test_counts_tallies_by_the_column_you_name(log):
    seed(log, time.time())
    assert log.counts(by="service") == {"arr": 2, "docker": 2, "github": 1, "pve": 1}
    assert log.counts(by="severity")["critical"] == 2
    assert log.counts(kind=ALERT_KINDS) == {"docker": 1, "pve": 1}
    assert log.counts(by="kind", service="arr") == {"grab": 1, "import": 1}
    assert log.counts(since=time.time() + HOUR) == {}


def test_counts_can_tally_the_numbers_table_so_a_page_knows_which_metrics_exist(log):
    log.sample(service="pve", metric="cpu", value=10, key="pve1")
    log.sample(service="pve", metric="cpu", value=20, key="pve2")
    log.sample(service="pve", metric="mem", value=30, key="pve1")
    log.sample(service="arr", metric="queue", value=4)
    assert log.counts(of="samples", by="metric") == {"cpu": 2, "mem": 1, "queue": 1}
    assert log.counts(of="samples", by="metric", service="pve") == {"cpu": 2, "mem": 1}
    assert log.counts(of="samples", by="key", service="pve") == {"pve1": 2, "pve2": 1}


def test_series_buckets_and_aggregates_the_numbers(log):
    now = time.time()
    base = now - now % HOUR                                  # line the points up inside two clean hours
    for i, value in enumerate((10.0, 20.0, 30.0)):
        log.sample(service="pve", metric="cpu", value=value, key="pve1", at=base - 2 * HOUR + i * 60)
    log.sample(service="pve", metric="cpu", value=90.0, key="pve1", at=base - HOUR + 60)
    log.sample(service="pve", metric="cpu", value=1.0, key="pve2", at=base - HOUR + 60)

    points = log.series(service="pve", metric="cpu", key="pve1", bucket=HOUR)
    assert [v for _, v in points] == [20.0, 90.0]            # the first hour averaged, the second on its own
    assert [t for t, _ in points] == [base - 2 * HOUR, base - HOUR]
    assert [v for _, v in log.series(service="pve", metric="cpu", key="pve1", bucket=HOUR, agg="max")] == \
        [30.0, 90.0]
    assert [v for _, v in log.series(service="pve", metric="cpu", bucket=HOUR)] == [20.0, 45.5]  # both nodes
    assert log.series(service="pve", metric="cpu", key="pve1", since=base) == []
    assert log.series(service="pve", metric="nothing") == []


def test_summarise_tallies_events_already_in_hand_the_same_way(log):
    seed(log, time.time())
    assert summarise(log.events()) == log.counts(by="service")


def test_window_is_the_last_n_days(log):
    since, until = window(2, now=1000 * DAY)
    assert until - since == 2 * DAY and until == 1000 * DAY


# ----- uptime ------------------------------------------------------------------------------------------
def test_uptime_counts_the_share_of_the_window_spent_up(log):
    now = time.time()
    since = now - 10 * HOUR
    log.record(service="docker", kind="up", key="sonarr", at=since - HOUR)      # up before the window opens
    log.record(service="docker", kind="down", key="sonarr", at=since + 2 * HOUR)
    log.record(service="docker", kind="up", key="sonarr", at=since + 4 * HOUR)  # two hours down of ten
    assert log.uptime(service="docker", key="sonarr", since=since, until=now) == pytest.approx(80.0)


def test_uptime_of_a_thing_that_never_came_back_runs_to_the_end_of_the_window(log):
    now = time.time()
    since = now - 4 * HOUR
    log.record(service="docker", kind="up", key="radarr", at=since - HOUR)
    log.record(service="docker", kind="down", key="radarr", at=since + HOUR)
    assert log.uptime(service="docker", key="radarr", since=since, until=now) == pytest.approx(25.0)


def test_uptime_assumes_the_opposite_state_before_the_first_event_it_can_see(log):
    now = time.time()
    since = now - 4 * HOUR
    log.record(service="docker", kind="down", key="lidarr", at=since + 3 * HOUR)   # nothing precedes it
    assert log.uptime(service="docker", key="lidarr", since=since, until=now) == pytest.approx(75.0)


def test_uptime_is_none_when_nothing_is_known_and_a_hundred_when_nothing_went_wrong(log):
    now = time.time()
    assert log.uptime(service="docker", key="never-seen", since=now - DAY, until=now) is None
    log.record(service="docker", kind="up", key="prowlarr", at=now - 2 * DAY)
    assert log.uptime(service="docker", key="prowlarr", since=now - DAY, until=now) == pytest.approx(100.0)
    assert log.uptime(service="docker", key="prowlarr", since=now, until=now) is None   # an empty window


def test_uptime_ignores_events_that_are_not_up_or_down(log):
    now = time.time()
    log.record(service="docker", kind="up", key="bazarr", at=now - 2 * DAY)
    log.record(service="docker", kind="note", key="bazarr", at=now - HOUR)
    assert log.uptime(service="docker", key="bazarr", since=now - DAY, until=now) == pytest.approx(100.0)


# ----- the no-op stand-in ------------------------------------------------------------------------------
def test_the_runtime_picks_this_up_and_falls_back_when_it_cannot(tmp_path):
    real = history_for(tmp_path / "history.db", 30)
    try:
        assert isinstance(real, History) and real.enabled and real.retention_days == 30
    finally:
        real.close()
    # a path that cannot hold a database: the bots must still run, on the no-op log
    bad = tmp_path / "afile"
    bad.write_text("not a database")
    assert isinstance(history_for(bad / "history.db", 30), NullHistory)


# ----- the recap ---------------------------------------------------------------------------------------
def test_the_digest_reads_a_seeded_night(log):
    now = time.time()
    seed(log, now)
    data = digest_ctx(log, now - 8 * HOUR, now, ())
    assert data["total"] == 6 and not data["quiet"] and data["hours"] == 8.0
    assert data["by_service"] == {"arr": 2, "docker": 2, "github": 1, "pve": 1}
    assert [a["title"] for a in data["recovered"]] == ["sonarr exited"]
    assert data["recovered"][0]["down_s"] == pytest.approx(HOUR, abs=5)
    assert [a["title"] for a in data["still_open"]] == ["High CPU on pve1"]
    assert [g["title"] for g in data["grabs"]] == ["Grabbed: The Expanse S06E01"]
    assert [i["title"] for i in data["imports"]] == ["Imported: The Expanse S06E01"]
    assert [c["key"] for c in data["ci"]] == ["anthill"]


def test_the_digest_embed_says_what_happened(log):
    now = time.time()
    seed(log, now)
    e = build_digest(log, now - 8 * HOUR, now, ())
    assert "While you were asleep" in e.title
    assert "6 things" in e.description and "1 alert still open" in e.description
    fields = {f.name: f.value for f in e.fields}
    assert fields["By service"].startswith("**arr** 2")
    assert "sonarr exited" in fields["Alerts (2)"] and "still open" in fields["Alerts (2)"]
    assert "The Expanse" in fields["Media (1 grabbed, 1 imported)"]
    assert "anthill" in fields["Builds that failed (1)"]


def test_the_digest_of_a_quiet_night_says_so(log):
    now = time.time()
    e = build_digest(log, now - 8 * HOUR, now, ())
    assert "quiet night" in (e.description or "")
    assert e.fields == []


def test_the_digest_only_covers_the_servers_it_was_asked_about(log):
    now = time.time()
    log.record(service="docker", kind="down", key="a", title="on the lab", server="testlab", at=now - HOUR)
    log.record(service="plexrequests", kind="request", title="on plex", server="Plex land", at=now - HOUR)
    log.record(service="arr", kind="grab", title="on neither in particular", at=now - HOUR)
    data = digest_ctx(log, now - DAY, now, {"main": {"name": "testlab"}})
    assert data["total"] == 2                                # the lab's, plus the one that named no server
    assert digest_ctx(log, now - DAY, now, ["testlab", "Plex land"])["total"] == 3
    assert digest_ctx(log, now - DAY, now, ())["total"] == 3


def test_an_alert_is_paired_with_the_resolve_that_follows_it_not_one_before():
    alerts = [{"ts": 100, "service": "pve", "key": "cpu", "title": "hot", "severity": "warning"}]
    early = [{"ts": 50, "service": "pve", "key": "cpu", "title": "cool"}]
    late = [{"ts": 150, "service": "pve", "key": "cpu", "title": "cool"}]
    assert alert_story(alerts, early)[0]["resolved"] is False
    assert alert_story(alerts, late)[0]["resolved"] is True
    assert alert_story(alerts, late)[0]["down_s"] == 50
    other = [{"ts": 150, "service": "pve", "key": "memory", "title": "cool"}]
    assert alert_story(alerts, other)[0]["resolved"] is False


def test_the_recap_is_editable_on_the_messages_page():
    (kind,) = digest_kinds()
    assert kind.key == DIGEST_KIND and REGISTRY[DIGEST_KIND] is not None
    assert kind.where_env == "STATUS_CHANNEL_ID" and kind.group == "boards"
    embed, ctx = kind.sample()
    assert "While you were asleep" in embed.title and ctx["total"] == 6
    assert "alerts" in kind.variables and "grabs" in kind.variables


def test_the_digest_reads_the_no_op_log_without_complaining():
    e = build_digest(NullHistory(), 0, 8 * HOUR, ())
    assert "quiet night" in (e.description or "")


# ----- the schedule ------------------------------------------------------------------------------------
def test_the_schedule_fires_once_a_day_at_the_hour():
    schedule = DigestSchedule(hour=8)
    day = time.mktime(time.struct_time((2026, 9, 4, 0, 0, 0, 4, 247, -1)))
    assert schedule.due(day + 7 * HOUR) is False           # before the hour
    assert schedule.due(day + 8 * HOUR) is True
    schedule.mark(day + 8 * HOUR)
    assert schedule.due(day + 9 * HOUR) is False           # already sent this morning
    assert schedule.due(day + 32 * HOUR) is True           # tomorrow morning


def test_the_schedule_remembers_across_a_restart(tmp_path):
    from periscope.state import JsonState

    state = JsonState(tmp_path / "state.json").namespace("digest")
    day = time.mktime(time.struct_time((2026, 9, 4, 0, 0, 0, 4, 247, -1)))
    DigestSchedule(hour=8, state=state).mark(day + 8 * HOUR)
    assert DigestSchedule(hour=8, state=state).due(day + 9 * HOUR) is False


def test_the_window_is_since_the_last_recap_and_never_longer_than_the_cap():
    schedule = DigestSchedule(hour=8, max_window_h=36)
    now = 1_000_000.0
    assert schedule.window(now)[0] == pytest.approx(now - 36 * HOUR)     # nothing remembered yet: the cap
    schedule.mark(now - 24 * HOUR)
    assert schedule.window(now)[0] == pytest.approx(now - 24 * HOUR)     # since the last one
    schedule.mark(now - 500 * HOUR)
    assert schedule.window(now)[0] == pytest.approx(now - 36 * HOUR)     # a long absence is still capped
