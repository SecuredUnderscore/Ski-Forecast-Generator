"""Service posting policy (no network, Discord disabled)."""
import sys
import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import skiforecast.service as service_mod
from skiforecast.config import PostConfig, SnowChartConfig
from skiforecast.service import Service, due_slot
from skiforecast.store import Store

from test_assemble import fake_run, make_cfg

UTC = timezone.utc
VAN = ZoneInfo("America/Vancouver")


def local(*a):
    return datetime(*a).replace(tzinfo=VAN).astimezone(UTC)


# --------------------------------------------------------------------------- due_slot
LA = ZoneInfo("America/Los_Angeles")


def la(*a):
    return datetime(*a).replace(tzinfo=LA).astimezone(UTC)


@pytest.mark.parametrize("tz, now, expected", [
    (VAN, local(2026, 11, 10, 5, 59), local(2026, 11, 9, 17, 0)),   # just before 06:00 -> yesterday 17:00
    (VAN, local(2026, 11, 10, 6, 0), local(2026, 11, 10, 6, 0)),    # exactly on the slot
    (VAN, local(2026, 11, 10, 12, 0), local(2026, 11, 10, 6, 0)),
    (VAN, local(2026, 11, 10, 23, 59), local(2026, 11, 10, 17, 0)),
    # BC stays on UTC-7 from Nov 2026 (permanent daylight time in current tzdata)
    (VAN, local(2026, 11, 2, 5, 59), datetime(2026, 11, 2, 0, 0, tzinfo=UTC)),
    # Los Angeles: DST ends 2026-11-01 02:00 PDT -> PST
    (LA, la(2026, 10, 31, 6, 30), datetime(2026, 10, 31, 13, 0, tzinfo=UTC)),   # PDT, UTC-7
    (LA, la(2026, 11, 1, 5, 59), datetime(2026, 11, 1, 0, 0, tzinfo=UTC)),      # Oct 31 17:00 PDT
    (LA, la(2026, 11, 1, 6, 0), datetime(2026, 11, 1, 14, 0, tzinfo=UTC)),      # PST, UTC-8
    (LA, la(2026, 11, 2, 1, 0), datetime(2026, 11, 2, 1, 0, tzinfo=UTC)),       # Nov 1 17:00 PST
    # spring forward 2026-03-08: 06:00 PST on Mar 7 vs 06:00 PDT on Mar 8
    (LA, la(2026, 3, 8, 6, 30), datetime(2026, 3, 8, 13, 0, tzinfo=UTC)),
    (LA, la(2026, 3, 8, 5, 30), datetime(2026, 3, 8, 1, 0, tzinfo=UTC)),        # Mar 7 17:00 PST
])
def test_due_slot(tz, now, expected):
    got = due_slot(["06:00", "17:00"], now, tz)
    assert got == expected and got.tzinfo is not None and got.utcoffset().total_seconds() == 0


# --------------------------------------------------------------------------- helpers
@pytest.fixture
def svc_factory(tmp_path, monkeypatch):
    # compute_needs only matters for downloading; keep these tests independent of it
    monkeypatch.setattr(service_mod, "compute_needs", lambda cfg: {})

    def make(**general):
        cfg = make_cfg(tmp_path)
        for k, v in general.items():
            setattr(cfg.general, k, v)
        return Service(cfg)
    return make


def save(svc, model, init):
    if model == "hrdps":
        run = fake_run("hrdps", init, 48, 3, 1, ["whistler_peak@0,0"])
    else:
        run = fake_run("gdps", init, 240, 3, 3, ["whistler_peak@-1,-1"], fl=1500)
    svc.store.save_run(model, run, svc.cfg.general.keep_runs)


def long_term(svc, **post):
    chart = next(c for c in svc.cfg.charts if c.id == "long_term")
    return chart.model_copy(update={"post": PostConfig(**post)})


# --------------------------------------------------------------------------- update mode
def test_update_mode_only_trigger_models_post(svc_factory):
    svc = svc_factory(post_on_startup=True)
    chart = long_term(svc, models=["hrdps"])
    save(svc, "hrdps", datetime(2026, 11, 6, 0, tzinfo=UTC))
    save(svc, "gdps", datetime(2026, 11, 5, 12, tzinfo=UTC))
    now = datetime(2026, 11, 6, 6, tzinfo=UTC)

    svc.maybe_post(chart, set(), now)                        # first cycle: posts
    cs = svc.store.chart_state(chart.id)
    assert cs["signature"] == "hrdps:20261106T00Z,gdps:20261105T12Z"
    assert cs["posted_at"] == now.isoformat()
    svc.first_cycle = False

    save(svc, "gdps", datetime(2026, 11, 6, 0, tzinfo=UTC))  # non-trigger model only
    svc.maybe_post(chart, {"gdps"}, now.replace(hour=8))
    assert cs["signature"] == "hrdps:20261106T00Z,gdps:20261105T12Z"
    assert cs["posted_at"] == now.isoformat()

    save(svc, "hrdps", datetime(2026, 11, 6, 6, tzinfo=UTC))  # trigger model
    later = now.replace(hour=12)
    svc.maybe_post(chart, {"hrdps"}, later)
    assert cs["signature"] == "hrdps:20261106T06Z,gdps:20261106T00Z"
    assert cs["posted_at"] == later.isoformat()


def test_update_mode_startup_silent(svc_factory):
    svc = svc_factory(post_on_startup=False)
    chart = long_term(svc)
    save(svc, "hrdps", datetime(2026, 11, 6, 0, tzinfo=UTC))
    svc.maybe_post(chart, set(), datetime(2026, 11, 6, 6, tzinfo=UTC))
    cs = svc.store.chart_state(chart.id)
    assert cs["signature"] == "hrdps:20261106T00Z" and "posted_at" not in cs


# --------------------------------------------------------------------------- schedule mode
def test_schedule_mode(svc_factory):
    svc = svc_factory(post_on_startup=False)
    chart = long_term(svc, times=["06:00"])
    save(svc, "hrdps", datetime(2026, 11, 10, 0, tzinfo=UTC))
    save(svc, "gdps", datetime(2026, 11, 9, 12, tzinfo=UTC))
    cs = svc.store.chart_state(chart.id)

    svc.maybe_post(chart, set(), local(2026, 11, 10, 5, 0))   # first cycle: record silently
    assert cs["scheduled_slot"] == local(2026, 11, 9, 6, 0).isoformat()
    assert "posted_at" not in cs
    svc.first_cycle = False

    svc.maybe_post(chart, set(), local(2026, 11, 10, 5, 30))  # nothing due
    assert "posted_at" not in cs

    t = local(2026, 11, 10, 6, 10)
    svc.maybe_post(chart, set(), t)                           # slot due -> post
    assert cs["scheduled_slot"] == local(2026, 11, 10, 6, 0).isoformat()
    assert cs["posted_at"] == t.isoformat()
    assert cs["signature"] == "hrdps:20261110T00Z,gdps:20261109T12Z"

    save(svc, "hrdps", datetime(2026, 11, 10, 6, tzinfo=UTC))  # model updates never post by themselves
    svc.maybe_post(chart, {"hrdps"}, local(2026, 11, 10, 6, 20))
    assert cs["posted_at"] == t.isoformat()
    assert cs["signature"] == "hrdps:20261110T00Z,gdps:20261109T12Z"

    t2 = local(2026, 11, 11, 6, 5)                            # next day
    svc.maybe_post(chart, set(), t2)
    assert cs["scheduled_slot"] == local(2026, 11, 11, 6, 0).isoformat()
    assert cs["posted_at"] == t2.isoformat()
    assert cs["signature"] == "hrdps:20261110T06Z,gdps:20261109T12Z"


def test_schedule_mode_startup_posts_and_throttle_keeps_slot_open(svc_factory):
    svc = svc_factory(post_on_startup=True)
    chart = long_term(svc, times=["06:00", "17:00"]).model_copy(update={"min_post_interval_minutes": 120})
    save(svc, "hrdps", datetime(2026, 11, 10, 0, tzinfo=UTC))
    cs = svc.store.chart_state(chart.id)

    t = local(2026, 11, 10, 16, 0)
    svc.maybe_post(chart, set(), t)                           # startup: posts immediately
    assert cs["posted_at"] == t.isoformat()
    assert cs["scheduled_slot"] == local(2026, 11, 10, 6, 0).isoformat()
    svc.first_cycle = False

    svc.maybe_post(chart, set(), local(2026, 11, 10, 17, 5))  # throttled: slot stays open
    assert cs["scheduled_slot"] == local(2026, 11, 10, 6, 0).isoformat()
    t2 = local(2026, 11, 10, 18, 1)
    svc.maybe_post(chart, set(), t2)
    assert cs["scheduled_slot"] == local(2026, 11, 10, 17, 0).isoformat()
    assert cs["posted_at"] == t2.isoformat()


def test_schedule_mode_without_data_marks_slot(svc_factory):
    svc = svc_factory(post_on_startup=True)
    chart = long_term(svc, times=["06:00"])
    svc.maybe_post(chart, set(), local(2026, 11, 10, 7, 0))
    cs = svc.store.chart_state(chart.id)
    assert cs["scheduled_slot"] == local(2026, 11, 10, 6, 0).isoformat() and "posted_at" not in cs


# --------------------------------------------------------------------------- blended snow dispatch
def test_snow_chart_dispatch(svc_factory, monkeypatch):
    svc = svc_factory(post_on_startup=True)
    chart = next((c for c in svc.cfg.charts if isinstance(c, SnowChartConfig)), None)
    if chart is None:
        chart = SnowChartConfig.model_validate({
            "id": "snow", "type": "blended_snow", "title": "Snow",
            "sources": [{"model": "hrdps", "point": "whistler_peak"}],
            "elevations": [2182, 675]})
    chart = chart.model_copy(update={"blend": chart.blend.model_copy(update={"lagged_runs": 2})})
    assert svc.chart_models(chart) == ["hrdps"]

    calls = {}

    def build(cfg, ch, runs, now=None):
        calls["runs"] = runs
        return types.SimpleNamespace(title="Snow", sources=[], recipe="Blend of 2 runs")

    def render(data, path):
        path.write_bytes(b"png")
        calls["path"] = path
        return path

    monkeypatch.setitem(sys.modules, "skiforecast.blend",
                        types.SimpleNamespace(build_snow_chart_data=build))
    monkeypatch.setitem(sys.modules, "skiforecast.snowchart",
                        types.SimpleNamespace(render_snow_chart=render))

    def recent_runs(self, model, n):
        calls.setdefault("recent", []).append((model, n))
        return [self.latest_run(model)] if self.latest_run(model) else []
    monkeypatch.setattr(Store, "recent_runs", recent_runs, raising=False)

    posted = {}
    monkeypatch.setattr(svc.discord, "post_chart",
                        lambda url, image, title, desc, content="": posted.update(desc=desc) or False)

    save(svc, "hrdps", datetime(2026, 11, 10, 0, tzinfo=UTC))
    path, data, latest = svc.render(chart)
    assert calls["recent"] == [("hrdps", 2)]
    assert list(calls["runs"]) == ["hrdps"] and calls["runs"]["hrdps"][0]["run"] == "20261110T00Z"
    assert latest["hrdps"]["run"] == "20261110T00Z"
    assert (path.parent / "latest.png").exists()

    svc.maybe_post(chart, set(), datetime(2026, 11, 10, 6, tzinfo=UTC))
    assert svc.store.chart_state(chart.id)["signature"] == "hrdps:20261110T00Z"
    assert "**Updated:**" in posted["desc"] and "Blend of 2 runs" in posted["desc"]


# --------------------------------------------------------------------------- datamart congestion
def test_datamart_busy_is_retried_quietly(svc_factory, monkeypatch):
    from skiforecast.datamart import DatamartBusy
    svc = svc_factory()
    svc.needs = {"hrdps": object()}
    init = datetime(2026, 11, 7, 12, tzinfo=UTC)
    tried = []
    monkeypatch.setattr(svc.dm, "candidate_runs", lambda m, now: [init, init - timedelta(hours=6)])

    def busy(m, needs, i):
        tried.append(i)
        raise DatamartBusy("dd.weather.gc.ca: Read timed out.")
    monkeypatch.setattr(svc.dm, "check_complete", busy)
    alerts = []
    monkeypatch.setattr(svc, "alert", lambda *a, **k: alerts.append(a))
    assert svc.update_model("hrdps", init + timedelta(hours=4)) is False
    assert tried == [init]          # doesn't fall back to downloading an older run
    assert not alerts
