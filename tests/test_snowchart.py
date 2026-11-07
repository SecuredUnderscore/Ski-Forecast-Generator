import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from skiforecast.chartdata import (  # noqa: E402
    ElevationPanel, LevelBand, SnowBar, SnowChartData, SnowChartStyle, SnowTotal, SourceInfo, Transition,
)
from skiforecast.snowchart import render_snow_chart  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 11, 7, 7, tzinfo=UTC)      # local midnight (UTC-7)
TZ = "America/Vancouver"
HOURS = 48
BAR_H = 3
ELEVATIONS = [(2200, "Peak"), (1650, "Mid"), (1100, "Base")]


def _snow_level(h: float) -> float:
    """Warm start near 1900 m, cooling to ~900 m behind the front."""
    return 900 + 1000 / (1 + math.exp((h - 20) / 5))


def _precip(h: float) -> float:
    """mm per 3 h at base: storm peaking around +18 h, showery tail."""
    return 7 * math.exp(-((h - 18) / 9) ** 2) + 1.2 * math.exp(-((h - 36) / 6) ** 2)


def _bars(elev: float) -> list[SnowBar]:
    bars = []
    for i in range(HOURS // BAR_H):
        h0 = i * BAR_H
        hm = h0 + BAR_H / 2
        mm = _precip(hm) * (1 + (elev - 1100) / 2000)
        p = min(1.0, max(0.0, 0.5 + (elev - _snow_level(hm) + 200) / 500))
        spread = 0.25 + 0.5 * h0 / HOURS
        snow = mm * p * 1.1
        rain = mm * (1 - p)
        m, ml = ("hrdps", "HRDPS") if h0 < 36 else ("gdps", "GDPS")
        s = START + timedelta(hours=h0)
        bars.append(SnowBar(s, s + timedelta(hours=BAR_H),
                            snow, snow * (1 - spread), snow * (1 + 1.3 * spread),
                            rain, rain * (1 - spread), rain * (1 + 1.3 * spread),
                            p, 0.2 < p < 0.8, m, ml))
    return bars


def _totals(bars: list[SnowBar]) -> list[SnowTotal]:
    out = []
    for d in range(HOURS // 24):
        s = START + timedelta(hours=24 * d)
        e = s + timedelta(hours=24)
        bs = [b for b in bars if s <= b.start < e]
        sc, rc = sum(b.snow_cm for b in bs), sum(b.rain_mm for b in bs)
        out.append(SnowTotal(s, e, sc, sc * 0.75, sc * 1.35, rc, rc * 0.75, rc * 1.35, complete=True))
    return out


def sample_data() -> SnowChartData:
    panels = []
    for elev, label in ELEVATIONS:
        bars = _bars(elev)
        panels.append(ElevationPanel(elev, label, bars, _totals(bars)))
    hrs = list(range(HOURS + 1))
    sl = [_snow_level(h) for h in hrs]
    band = LevelBand(
        times=[START + timedelta(hours=h) for h in hrs],
        snow_level=sl,
        snow_level_lo=[v - 120 - 4 * h for v, h in zip(sl, hrs)],
        snow_level_hi=[v + 120 + 4 * h for v, h in zip(sl, hrs)],
        freezing_level=[v + 350 for v in sl],
    )
    return SnowChartData(
        chart_id="blended_snow", title="Whistler — Blended Snow Forecast (48 h)",
        subtitle="Snow level · Snow & rain by elevation", timezone=TZ, start=START,
        end=START + timedelta(hours=HOURS), generated=START + timedelta(hours=2),
        panels=panels, level_band=band, bar_hours=BAR_H, total_hours=24,
        transitions=[Transition(START + timedelta(hours=36), "HRDPS→GDPS")],
        sources=[SourceInfo("Snow + snow level", "HRDPS", START - timedelta(hours=8), "Whistler Peak",
                            50.067, -122.949, 1790, "+8–44h"),
                 SourceInfo("Snow + snow level", "GDPS", START - timedelta(hours=8), "Whistler Peak (-1,-1)",
                            49.95, -123.1, 1240, "+44–56h")],
        recipe="Blend: HRDPS + RDPS + GDPS weighted by lead time; spread = widened 10–90 % of members; "
               "snow level = wet-bulb 0 °C − 200 m",
    )


def test_render_full(tmp_path):
    out = render_snow_chart(sample_data(), tmp_path / "s.png")
    assert out.exists() and out.stat().st_size > 20_000


def test_render_empty(tmp_path):
    d = SnowChartData("empty", "Empty", "", TZ, START, START + timedelta(hours=48), START)
    assert render_snow_chart(d, tmp_path / "e.png").exists()


def test_render_all_none_level_band(tmp_path):
    d = sample_data()
    n = len(d.level_band.times)
    d.level_band = LevelBand(d.level_band.times, [None] * n, [None] * n, [None] * n, [None] * n)
    d.panels[0].bars[3].snow_cm = None
    d.panels[0].bars[4].rain_hi = float("nan")
    assert render_snow_chart(d, tmp_path / "n.png").exists()
    d.level_band = LevelBand([], [], [], [], [])
    assert render_snow_chart(d, tmp_path / "n2.png").exists()


def test_render_bar_ranges(tmp_path):
    d = sample_data()
    d.style = SnowChartStyle(show_bar_ranges=True)
    assert render_snow_chart(d, tmp_path / "r.png").exists()


if __name__ == "__main__":
    # Quick look at the synthetic sample: `python tests/test_snowchart.py out.png [ranges]`.
    # The README example (docs/example_blended_snow.png) is rendered from real data with
    # `skiforecast render snow_2day`, not from this sample.
    d = sample_data()
    if "ranges" in sys.argv[2:]:
        d.style = SnowChartStyle(show_bar_ranges=True)
    render_snow_chart(d, Path(sys.argv[1] if len(sys.argv) > 1 else "sample_blended_snow.png"))
