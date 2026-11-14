"""Blended Snow Forecast assembly from synthetic stored runs (no network)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import yaml

from skiforecast.blend import build_snow_chart_data
from skiforecast.chartdata import SnowChartData
from skiforecast.config import load_config

UTC = timezone.utc
LEVELS = [1015, 1000, 950, 900, 850, 800, 750, 700, 650, 600]
HEIGHTS = [0, 110, 540, 990, 1460, 1950, 2470, 3010, 3590, 4210]
NOW = datetime(2026, 11, 6, 4, 30, tzinfo=UTC)
PK = "whistler_peak@0,0"


def _cell(prof_hours, pr_hours, fl, rate, dt=0.0, psfc=880.0):
    rows = lambda f: [[f(h, z) for z in HEIGHTS] for h in prof_hours]
    prof = {
        "surface_pressure_hpa": [psfc] * len(prof_hours),
        "temperature": rows(lambda h, z: (fl - z) * 0.0065 + dt),
        "height": rows(lambda h, z: z),
        "dewpoint_depression": rows(lambda h, z: 1.0),
        "wind_speed": rows(lambda h, z: 30.0),
        "wind_direction": rows(lambda h, z: 240.0),
    }
    return prof, [rate * h for h in pr_hours]


def fake_run(model, init, max_hour, prof_step, precip_step, pkeys, fl=1800.0, rate=0.5, radius=1,
             neighbours=True):
    """Stored run in the extractor's format, with a (2r+1)^2 neighbourhood whose cells have
    slightly different precipitation, temperature and terrain."""
    prof_hours = list(range(0, max_hour + 1, prof_step))
    pr_hours = [0] + list(range(precip_step, max_hour + 1, precip_step))
    points, pdata, rdata, pnb, rnb = {}, {}, {}, {}, {}
    for pk in pkeys:
        cells = [[dj, di, 0] for dj in range(-radius, radius + 1) for di in range(-radius, radius + 1)]
        points[pk] = {"grid_lat": 50.0, "grid_lon": -123.0, "index": 0, "radius_cells": radius,
                      "spacing_km": 2.5, "cells": cells}
        pdata[pk], rdata[pk] = _cell(prof_hours, pr_hours, fl, rate)
        if neighbours:
            pnb[pk], rnb[pk] = {}, {}
            for dj, di, _ in cells:
                if (dj, di) == (0, 0):
                    continue
                k = f"{dj},{di}"
                pnb[pk][k], rnb[pk][k] = _cell(prof_hours, pr_hours, fl, rate * (1 + 0.1 * dj),
                                               dt=0.3 * di, psfc=880.0 + 10 * dj)
    run = {"model": model, "run": init.strftime("%Y%m%dT%HZ"), "levels": LEVELS, "signature": "sig",
           "points": points,
           "profile": {"hours": prof_hours, "data": pdata},
           "precip": {"hours": pr_hours, "data": rdata}}
    if neighbours:
        run["profile"]["neighbours"] = pnb
        run["precip"]["neighbours"] = rnb
    return run


def make_cfg(tmp_path: Path):
    src = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
    raw = yaml.safe_load(src.read_text(encoding="utf-8"))
    raw["general"]["data_dir"] = str(tmp_path)
    raw["discord"]["enabled"] = False
    raw["discord"]["default_webhook"] = None
    raw["discord"]["alerts_webhook"] = None
    raw["charts"].append({
        "type": "blended_snow", "id": "snow", "title": "Snow ({models})", "hours": 48,
        "sources": [{"model": "hrdps", "point": "whistler_peak"}],
        "elevations": [{"elevation": 2200, "label": "Peak"}, 1100], "bar_hours": 3})
    raw["charts"].append({
        "type": "blended_snow", "id": "snow_long", "title": "Snow ({models})", "hours": 96,
        "sources": [{"model": "hrdps", "point": "whistler_peak", "until_hour": 24},
                    {"model": "gdps", "point": "whistler_peak"}],
        "elevations": [2200, 1100], "bar_hours": 6})
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_config(p)


def _chart(cfg, cid="snow"):
    return next(c for c in cfg.charts if c.id == cid)


def _hrdps(init=datetime(2026, 11, 6, 0, tzinfo=UTC), **kw):
    return fake_run("hrdps", init, 48, 3, 1, [PK], **kw)


def test_phase_by_elevation(tmp_path):
    cfg = make_cfg(tmp_path)
    d = build_snow_chart_data(cfg, _chart(cfg), {"hrdps": [_hrdps()]}, NOW)
    assert isinstance(d, SnowChartData)
    peak, low = d.panels
    assert peak.elevation_m == 2200 and peak.label == "Peak" and low.elevation_m == 1100
    assert peak.bars and low.bars
    for b in peak.bars[1:]:
        assert b.p_snow > 0.8 and b.rain_mm < 0.05 and b.snow_cm > 0
    for b in low.bars[1:]:
        assert b.p_snow < 0.2 and b.snow_cm < 0.05 and b.rain_mm > 0
    assert d.title == "Snow (HRDPS)"
    assert "HRDPS 1 run × 9 cells (3×3, 2.5 km)" in d.recipe and "9 phase/ratio" in d.recipe
    assert d.sources and d.sources[0].role.startswith("Snow blend (1 run × 9 cells)")


def test_ranges_are_ordered(tmp_path):
    cfg = make_cfg(tmp_path)
    d = build_snow_chart_data(cfg, _chart(cfg), {"hrdps": [_hrdps(fl=2600)]}, NOW)
    for p in d.panels:
        for b in p.bars:
            assert 0 <= b.snow_lo <= b.snow_cm <= b.snow_hi
            assert 0 <= b.rain_lo <= b.rain_mm <= b.rain_hi
            assert 0 <= b.p_snow <= 1
        for t in p.totals:
            assert 0 <= t.snow_lo <= t.snow_cm <= t.snow_hi
            assert 0 <= t.rain_lo <= t.rain_mm <= t.rain_hi
    assert any(b.mixed for p in d.panels for b in p.bars)


def test_lagged_runs_widen_spread(tmp_path):
    cfg = make_cfg(tmp_path)
    chart = _chart(cfg)
    t0 = datetime(2026, 11, 6, 0, tzinfo=UTC)
    newest = _hrdps(t0, rate=0.5)
    one = build_snow_chart_data(cfg, chart, {"hrdps": [newest]}, NOW)
    three = build_snow_chart_data(cfg, chart, {"hrdps": [
        newest, _hrdps(t0 - timedelta(hours=6), rate=1.0), _hrdps(t0 - timedelta(hours=12), rate=0.2)]}, NOW)
    width = lambda d: np.mean([b.snow_hi - b.snow_lo for b in d.panels[0].bars])
    assert width(three) > 1.2 * width(one)
    assert "3 runs × 9 cells" in three.recipe
    assert three.sources[0].role.startswith("Snow blend (3 runs")
    # a run with another grid signature is not blended
    other = _hrdps(t0 - timedelta(hours=6), rate=1.0)
    other["signature"] = "different"
    d = build_snow_chart_data(cfg, chart, {"hrdps": [newest, other]}, NOW)
    assert "1 run ×" in d.recipe


def test_totals(tmp_path):
    cfg = make_cfg(tmp_path)
    chart = _chart(cfg)
    d = build_snow_chart_data(cfg, chart, {"hrdps": [_hrdps()]}, NOW)
    peak = d.panels[0]
    assert peak.totals
    starts = {b.start: b for b in peak.bars}
    assert any(t.complete for t in peak.totals) and any(not t.complete for t in peak.totals)
    for t in peak.totals:
        slots = [t.start + timedelta(hours=3 * k) for k in range(8)]
        present = [starts[s] for s in slots if s in starts]
        assert t.complete == (len(present) == 8)
        s = sum(b.snow_cm for b in present)
        assert abs(t.snow_cm - s) <= 0.3 * s + 1e-9
        assert t.snow_hi - t.snow_cm >= max(0.2 * t.snow_cm, 2.0) - 1e-9


def test_level_band(tmp_path):
    cfg = make_cfg(tmp_path)
    d = build_snow_chart_data(cfg, _chart(cfg), {"hrdps": [_hrdps()]}, NOW)
    lb = d.level_band
    assert lb is not None and lb.times and lb.times == sorted(set(lb.times))
    assert all(d.start <= t <= d.end for t in lb.times)
    for s, lo, hi, f in zip(lb.snow_level, lb.snow_level_lo, lb.snow_level_hi, lb.freezing_level):
        assert s is not None and f is not None
        assert lo <= s <= hi and s < f
        assert abs(f - 1800) < 150


def test_no_runs(tmp_path):
    cfg = make_cfg(tmp_path)
    for runs in ({}, {"hrdps": []}):
        d = build_snow_chart_data(cfg, _chart(cfg), runs, NOW)
        assert isinstance(d, SnowChartData)
        assert len(d.panels) == 2 and all(not p.bars and not p.totals for p in d.panels)
        assert d.level_band is None and d.recipe == "" and "no data" in d.title


def test_old_runs_without_neighbours(tmp_path):
    cfg = make_cfg(tmp_path)
    d = build_snow_chart_data(cfg, _chart(cfg), {"hrdps": [_hrdps(neighbours=False)]}, NOW)
    assert d.panels[0].bars and "1 run × 1 cell" in d.recipe


def test_hand_over_to_gdps(tmp_path):
    cfg = make_cfg(tmp_path)
    chart = _chart(cfg, "snow_long")
    runs = {"hrdps": [_hrdps()],
            "gdps": [fake_run("gdps", datetime(2026, 11, 5, 12, tzinfo=UTC), 240, 3, 3, [PK], fl=1500)]}
    d = build_snow_chart_data(cfg, chart, runs, NOW)
    assert [t.label for t in d.transitions] == ["HRDPS→GDPS"]
    assert d.transitions[0].time == datetime(2026, 11, 7, 0, tzinfo=UTC)
    assert {b.model for b in d.panels[0].bars} == {"hrdps", "gdps"}
    assert "HRDPS→GDPS" in d.title
    assert len(d.sources) == 2 and "; GDPS 1 run" in d.recipe
    bars = d.panels[0].bars
    assert all(a.end == b.start for a, b in zip(bars, bars[1:]))


def test_start_at_run(tmp_path):
    cfg = make_cfg(tmp_path)
    chart = _chart(cfg)
    d = build_snow_chart_data(cfg, chart, {"hrdps": [_hrdps()]}, NOW)      # run 06 00Z, now 06 04:30Z
    assert d.start == datetime(2026, 11, 6, 0, tzinfo=UTC)
    assert d.end == datetime(2026, 11, 8, 0, tzinfo=UTC)                   # run start + hours
    chart.start_at_run = False
    d = build_snow_chart_data(cfg, chart, {"hrdps": [_hrdps()]}, NOW)
    assert d.start == datetime(2026, 11, 6, 4, tzinfo=UTC)                 # now, floored to the hour
