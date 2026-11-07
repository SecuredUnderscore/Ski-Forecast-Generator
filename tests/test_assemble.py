"""Chart assembly from synthetic stored runs (no network)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from skiforecast.assemble import build_chart_data
from skiforecast.chart import render_chart
from skiforecast.config import ChartConfig, load_config

UTC = timezone.utc
LEVELS = [1015, 1000, 950, 900, 850, 800, 750, 700, 650, 600]
HEIGHTS = [0, 110, 540, 990, 1460, 1950, 2470, 3010, 3590, 4210]


def fake_run(model, init, max_hour, prof_step, precip_step, pkeys, fl=1800, rate=0.5):
    prof_hours = list(range(0, max_hour + 1, prof_step))
    pr_hours = [0] + list(range(precip_step, max_hour + 1, precip_step))
    data, precip, points = {}, {}, {}
    for pk in pkeys:
        points[pk] = {"grid_lat": 50.0, "grid_lon": -123.0, "index": 0}
        rows = lambda f: [[f(h, z) for z in HEIGHTS] for h in prof_hours]
        data[pk] = {
            "surface_pressure_hpa": [880.0] * len(prof_hours),
            "temperature": rows(lambda h, z: (fl + 300 * ((h // 24) % 2) - z) * 0.0065),
            "height": rows(lambda h, z: z),
            "dewpoint_depression": rows(lambda h, z: 1.5),
            "wind_speed": rows(lambda h, z: 30.0),
            "wind_direction": rows(lambda h, z: 240.0),
        }
        precip[pk] = [rate * h for h in pr_hours]
    return {"model": model, "run": init.strftime("%Y%m%dT%HZ"), "levels": LEVELS, "signature": "",
            "points": points,
            "profile": {"hours": prof_hours, "data": data},
            "precip": {"hours": pr_hours, "data": precip}}


def make_cfg(tmp_path: Path):
    src = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
    raw = yaml.safe_load(src.read_text())
    raw["general"]["data_dir"] = str(tmp_path)
    raw["discord"]["enabled"] = False
    raw["discord"]["default_webhook"] = None
    raw["discord"]["alerts_webhook"] = None
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(raw))
    return load_config(p)


def test_long_term_hands_over_from_hrdps_to_gdps(tmp_path):
    cfg = make_cfg(tmp_path)
    chart = next(c for c in cfg.charts if c.id == "long_term")
    now = datetime(2026, 11, 6, 4, 30, tzinfo=UTC)
    runs = {
        "hrdps": fake_run("hrdps", datetime(2026, 11, 6, 0, tzinfo=UTC), 48, 3, 1, ["whistler_peak@0,0"]),
        "gdps": fake_run("gdps", datetime(2026, 11, 5, 12, tzinfo=UTC), 240, 3, 3, ["whistler_peak@-1,-1"], fl=1500),
    }
    d = build_chart_data(cfg, chart, runs, now)
    assert [s.model for s in d.freezing_level] == ["hrdps", "gdps"]
    hr, gd = d.freezing_level
    assert hr.times[-1] == datetime(2026, 11, 8, 0, tzinfo=UTC) == gd.times[0]
    assert abs(hr.freezing_level[0] - 1800) < 10
    assert all(w is not None and w < f for w, f in zip(hr.wetbulb_zero, hr.freezing_level))
    # 6 h bars: hrdps 0.5 mm/h -> 3 mm, gdps 0.5 mm/h -> 3 mm
    assert d.precip_bars and all(abs(b.mm - 3.0) < 1e-6 for b in d.precip_bars[1:])
    assert {b.model for b in d.precip_bars} == {"hrdps", "gdps"}
    assert d.precip_bars[-1].end <= d.end + timedelta(hours=6)
    assert any(t.complete and abs(t.mm - 12) < 1e-6 for t in d.precip_totals)
    assert "HRDPS→GDPS" in d.title
    render_chart(d, tmp_path / "long.png")


def test_fallback_when_first_model_missing(tmp_path):
    cfg = make_cfg(tmp_path)
    chart = next(c for c in cfg.charts if c.id == "high_2day")
    now = datetime(2026, 11, 6, 4, 30, tzinfo=UTC)
    runs = {"hrdps": fake_run("hrdps", datetime(2026, 11, 6, 0, tzinfo=UTC), 48, 3, 1, ["whistler_peak@0,0"])}
    d = build_chart_data(cfg, chart, runs, now)
    assert [s.model for s in d.freezing_level] == ["hrdps"]
    assert d.wind and abs(d.wind[0].speed_kmh - 30) < 1e-6
    render_chart(d, tmp_path / "high.png")


def test_no_data_renders(tmp_path):
    cfg = make_cfg(tmp_path)
    for chart in cfg.charts:
        if not isinstance(chart, ChartConfig):   # blended snow charts have their own builder
            continue
        d = build_chart_data(cfg, chart, {})
        render_chart(d, tmp_path / f"{chart.id}.png")


def test_trim_to_data_and_midnight_totals(tmp_path):
    cfg = make_cfg(tmp_path)
    chart = next(c for c in cfg.charts if c.id == "low_2day").model_copy(update={"hours": 72})
    now = datetime(2026, 11, 6, 4, 30, tzinfo=UTC)
    runs = {"hrdps": fake_run("hrdps", datetime(2026, 11, 6, 0, tzinfo=UTC), 48, 3, 1, ["whistler_village@0,0"])}
    d = build_chart_data(cfg, chart, runs, now)
    assert d.end == now.replace(minute=0) + timedelta(hours=72)
    d = build_chart_data(cfg, chart.model_copy(update={"trim_to_data": True}), runs, now)
    assert d.end == datetime(2026, 11, 8, 0, tzinfo=UTC)
    # Vancouver is UTC-7 from Nov 2026: local midnight = 07Z
    assert all(t.start.hour == 7 for t in d.precip_totals)
    assert all((b.start.hour - 7) % 3 == 0 for b in d.precip_bars)
    # totals follow the Zulu day marker when one is set
    chart.style = chart.style.model_copy(update={"tick_hour_utc": 12})
    d = build_chart_data(cfg, chart, runs, now)
    assert d.precip_totals and all(t.start.hour == 12 for t in d.precip_totals)
