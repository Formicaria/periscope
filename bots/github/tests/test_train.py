"""CI train: one live card per Actions run, edited in place job by job, finalized once."""

import pytest
from periscope.messages import Messages, MessageStore
from periscope.state import JsonState

from periscope_github.config import GithubSettings
from periscope_github.train import KIND, CiTrains, job_line, render_train


class Msg:
    def __init__(self, mid, embed):
        self.id, self.embeds = mid, [embed]

    async def edit(self, embed):
        self.embeds = [embed]


class Chan:
    def __init__(self):
        self.msgs = {}
        self.n = 0

    async def send(self, embed):
        self.n += 1
        self.msgs[self.n] = Msg(self.n, embed)
        return self.msgs[self.n]

    async def fetch_message(self, mid):
        return self.msgs[mid]


class Bot:
    def __init__(self, tmp_path, chan, messages=None):
        self.state = JsonState(tmp_path / "s.json")
        self.lab_name = "Formicaria"
        self.settings = type("S", (), {"alert_channel_id": None})()
        self.messages = messages or Messages()   # no customisations unless a test hands in a store
        self.chan = chan

    async def get_channel_safe(self, cid):
        return self.chan


class Client:
    def __init__(self):
        self.run = {"id": 9, "name": "ci", "status": "in_progress", "conclusion": None, "head_branch": "main",
                    "head_sha": "abcdef0", "display_title": "fix things", "run_number": 4, "html_url": "u",
                    "run_started_at": "2026-09-02T00:00:00Z", "updated_at": "2026-09-02T00:00:30Z",
                    "triggering_actor": {"login": "xchronusx"}}
        self.jobs = [
            {"name": "lint", "status": "completed", "conclusion": "success",
             "started_at": "2026-09-02T00:00:00Z", "completed_at": "2026-09-02T00:00:12Z"},
            {"name": "test (core)", "status": "in_progress", "steps": [
                {"name": "checkout", "status": "completed"}, {"name": "pytest", "status": "in_progress"},
                {"name": "upload", "status": "queued"}]},
            {"name": "docker", "status": "queued"},
        ]

    async def workflow_run(self, repo, run_id):
        return self.run

    async def workflow_run_jobs(self, repo, run_id):
        return self.jobs


def test_job_lines():
    c = Client()
    assert job_line(c.jobs[0]) == "✅ **lint**  12s"
    assert job_line(c.jobs[1]) == "🟡 **test (core)**  step 2/3 · pytest"
    assert job_line(c.jobs[2]) == "⚪ **docker**  queued"


@pytest.mark.asyncio
async def test_train_lifecycle(tmp_path):
    chan, client = Chan(), Client()
    bot = Bot(tmp_path, chan)
    done = []

    async def on_complete(repo, run):
        done.append((repo, run["conclusion"]))

    t = CiTrains(bot, client, GithubSettings(ci_channel_id=5), on_complete=on_complete)
    assert await t.start("periscope", client.run) is True
    assert await t.start("periscope", client.run) is False          # idempotent
    assert t.is_tracked(9) and chan.n == 1
    e = chan.msgs[1].embeds[0]
    assert "🟡 ci · run #4 on main" in e.title and "jobs 1/3" in e.description and "step 2/3 · pytest" in e.description

    # jobs move on → same message, new content
    client.jobs[1] = {**client.jobs[1], "status": "completed", "conclusion": "success",
                      "started_at": "2026-09-02T00:00:12Z", "completed_at": "2026-09-02T00:01:00Z"}
    client.jobs[2] = {**client.jobs[2], "status": "in_progress", "steps": []}
    await t.tick()
    e = chan.msgs[1].embeds[0]
    assert chan.n == 1 and "jobs 2/3" in e.description and "🟡 **docker**  running" in e.description

    # run finishes → final edit, completion hook once, tracking cleared
    client.jobs[2] = {**client.jobs[2], "status": "completed", "conclusion": "failure",
                      "started_at": "2026-09-02T00:01:00Z", "completed_at": "2026-09-02T00:02:00Z"}
    client.run = {**client.run, "status": "completed", "conclusion": "failure", "updated_at": "2026-09-02T00:02:00Z"}
    await t.tick()
    e = chan.msgs[1].embeds[0]
    assert chan.n == 1 and "❌ ci · run #4" in e.title and "failure in 2m" in e.description and "❌ **docker**  1m" in e.description
    assert done == [("periscope", "failure")] and not t.is_tracked(9)
    await t.tick()
    assert done == [("periscope", "failure")]                        # nothing tracked, nothing repeated


@pytest.mark.asyncio
async def test_train_card_goes_through_the_template_on_every_edit(tmp_path):
    chan, client = Chan(), Client()
    store = MessageStore(tmp_path / "messages.yaml")
    bot = Bot(tmp_path, chan, Messages(store, service="github", lab="Formicaria"))
    t = CiTrains(bot, client, GithubSettings(ci_channel_id=5))
    store.set(KIND, {"title": "🚂 {{ workflow }} #{{ run_number }} · {{ jobs_done }}/{{ job_count }} jobs",
                     "description": "{% for j in jobs %}{{ j.line }}\n{% endfor %}", "color": "auto",
                     "fields": [{"name": "Status", "value": "{{ status }}", "inline": True}], "timestamp": True})
    assert await t.start("periscope", client.run) is True
    e = chan.msgs[1].embeds[0]
    assert e.title == "🚂 ci #4 · 1/3 jobs" and e.description.startswith("✅ **lint**  12s\n🟡 **test (core)**")
    assert e.fields[0].value == "in_progress" and e.url is None       # the template did not keep the link

    client.jobs[1] = {**client.jobs[1], "status": "completed", "conclusion": "success",
                      "started_at": "2026-09-02T00:00:12Z", "completed_at": "2026-09-02T00:01:00Z"}
    await t.tick()
    assert chan.msgs[1].embeds[0].title == "🚂 ci #4 · 2/3 jobs"       # edits are rendered through it too

    store.set(KIND, None, enabled=False)                              # switched off mid-run: the card stays as it is
    client.run = {**client.run, "status": "completed", "conclusion": "success", "updated_at": "2026-09-02T00:02:00Z"}
    await t.tick()
    assert chan.msgs[1].embeds[0].title == "🚂 ci #4 · 2/3 jobs" and not t.is_tracked(9)
    assert await t.start("periscope", {**client.run, "id": 10, "status": "in_progress"}) is False   # and no new cards
    assert chan.n == 1
