import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skiforecast.chart import render_chart  # noqa: E402
from skiforecast.chartdata import (  # noqa: E402
    ChartData, FreezingLevelSegment, PrecipBar, PrecipTotal, ReferenceLine, SourceInfo, WindSample,
)

UTC = timezone.utc
START = datetime(2026, 11, 6, 6, tzinfo=UTC)
TZ = "America/Vancouver"


def _fl(h):
    return 1500 + 600 * math.sin(h / 30) + 400 * math.exp(-((h - 160) / 25) ** 2)


def sample_data() -> ChartData:
    end = START + timedelta(hours=240)
    hr = [START + timedelta(hours=h) for h in range(0, 49)]
    gd = [START + timedelta(hours=h) for h in range(51, 241, 3)]
    segs = [
        FreezingLevelSegment("hrdps", "HRDPS", START - timedelta(hours=6), "Roundhouse", hr,
                             [_fl(h) for h in range(0, 49)], [_fl(h) - 250 for h in range(0, 49)]),
        FreezingLevelSegment("gdps", "GDPS", START - timedelta(hours=18), "Roundhouse", gd,
                             [_fl(h) for h in range(51, 241, 3)], [_fl(h) - 250 for h in range(51, 241, 3)]),
    ]
    bars = []
    for i in range(40):
        s = START + timedelta(hours=6 * i)
        mm = max(0.0, 8 * math.sin(i / 3)) if i < 20 else (0.4 if i % 5 == 0 else 0.0)
        m, ml = ("hrdps", "HRDPS") if i < 8 else ("gdps", "GDPS")
        bars.append(PrecipBar(s, s + timedelta(hours=6), mm, m, ml))
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(TZ)
    totals = []
    d0 = START.astimezone(tz).replace(hour=0, minute=0)
    for d in range(11):
        s = (d0 + timedelta(days=d)).astimezone(UTC)
        e = s + timedelta(days=1)
        mm = sum(b.mm for b in bars if s <= b.start < e)
        totals.append(PrecipTotal(s, e, mm, complete=s >= START and e <= end))
    wind = [WindSample(START + timedelta(hours=h), 20 + 30 * abs(math.sin(h / 20)), (200 + h) % 360,
                       "hrdps" if h <= 48 else "gdps") for h in range(0, 241, 6)]
    return ChartData(
        chart_id="long_term", title="Whistler Blackcomb — 10-day Meteogram (HRDPS→GDPS)",
        subtitle="Freezing Level · Wind · Precipitation", timezone=TZ, start=START, end=end,
        generated=START + timedelta(hours=3), freezing_level=segs, precip_bars=bars, precip_totals=totals,
        precip_bar_hours=6, wind=wind, wind_elevation_m=2200,
        reference_lines=[ReferenceLine(675, "Village"), ReferenceLine(1850, "Roundhouse"),
                         ReferenceLine(2182, "Peak", color="#6a4a8a")],
        sources=[SourceInfo("Freezing level", "HRDPS", START - timedelta(hours=6), "Roundhouse", 50.067, -122.949,
                            1790, "0-48h"),
                 SourceInfo("Precipitation", "GDPS", START - timedelta(hours=18), "Roundhouse (-1,-1)", 49.95,
                            -123.1, None, "48-240h")],
    )


def test_render_full(tmp_path):
    out = render_chart(sample_data(), tmp_path / "c.png")
    assert out.exists() and out.stat().st_size > 10_000


def test_render_empty(tmp_path):
    d = ChartData("empty", "Empty", "", TZ, START, START + timedelta(hours=48), START)
    assert render_chart(d, tmp_path / "e.png").exists()


def test_render_all_none(tmp_path):
    d = sample_data()
    for s in d.freezing_level:
        s.freezing_level = [None] * len(s.times)
        s.wetbulb_zero = None
    d.wind = []
    d.precip_bars = []
    assert render_chart(d, tmp_path / "n.png").exists()


if __name__ == "__main__":
    render_chart(sample_data(), Path(__file__).resolve().parents[1] / "sample_chart.png")


def test_time_ticks():
    from zoneinfo import ZoneInfo
    from skiforecast.chart import _tick_step, _time_ticks, format_zulu_local
    assert [_tick_step(n) for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 11)] == [24, 12, 8, 6, 4, 4, 3, 3, 2, 2]
    tz = ZoneInfo(TZ)
    days, within = _time_ticks(START, START + timedelta(hours=48), tz, None, 6)
    assert [t.hour for t in days] == [0, 0] and {t.hour for t in within} == {6, 12, 18}
    days, within = _time_ticks(START, START + timedelta(hours=48), tz, 12, 6)
    assert [t.astimezone(UTC).hour for t in days] == [12, 12] and days[0].tzinfo == tz
    assert {t.astimezone(UTC).hour for t in within} == {18, 0, 6}
    assert format_zulu_local(datetime(2026, 11, 6, 0, tzinfo=UTC), tz) == "2026-11-06 00Z (Thu05 5pm MST)"
