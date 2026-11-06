"""Turn stored model runs + chart config into a renderable ChartData."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .chartdata import (ChartData, ChartStyle, FreezingLevelSegment, PrecipBar, PrecipTotal,
                        ReferenceLine, SourceInfo, WindSample)
from .config import AppConfig, ChartConfig, SourceConfig
from .datamart import parse_run_id, point_key
from . import meteo

UTC = timezone.utc


@dataclass
class Window:
    source: SourceConfig
    run: dict
    init: datetime
    start: datetime
    end: datetime
    pkey: str


def _floor(t: datetime, hours: int) -> datetime:
    t = t.replace(minute=0, second=0, microsecond=0)
    return t - timedelta(hours=t.hour % hours)


def plan_windows(cfg: AppConfig, sources: list[SourceConfig], runs: dict[str, dict], layer: str,
                 start: datetime, end: datetime) -> list[Window]:
    """Allocate consecutive time windows to the configured sources (in order).
    Sources without data are skipped, so later sources act as fallbacks."""
    cursor, out = start, []
    for s in sources:
        run = runs.get(s.model)
        pk = point_key(s.point, s.offset)
        if not run or not run.get(layer) or pk not in run[layer]["data"]:
            continue
        init = parse_run_id(run["run"])
        hours = run[layer]["hours"]
        last = max(hours)
        if s.until_hour is not None:
            last = min(last, s.until_hour)
        seg_end = min(init + timedelta(hours=last), end)
        if seg_end <= cursor:
            continue
        out.append(Window(s, run, init, cursor, seg_end, pk))
        cursor = seg_end
        if cursor >= end:
            break
    return out


def _label(cfg: AppConfig, model: str) -> str:
    return cfg.resolved_models[model].label


def _hours_str(w: Window) -> str:
    a = int((w.start - w.init).total_seconds() // 3600)
    b = int((w.end - w.init).total_seconds() // 3600)
    return f"+{max(a, 0)}–{b}h"


def build_chart_data(cfg: AppConfig, chart: ChartConfig, runs: dict[str, dict],
                     now: Optional[datetime] = None) -> ChartData:
    now = now or datetime.now(UTC)
    tz = cfg.general.timezone
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=chart.history_hours)
    end = start + timedelta(hours=chart.hours + chart.history_hours)

    sources: list[SourceInfo] = []

    def add_source(role: str, w: Window):
        pt = w.run["points"][w.pkey]
        prof = (w.run.get("profile") or {}).get("data", {}).get(w.pkey)
        elev = None
        if prof:
            # median over the run: the analysis hour can use different terrain
            zs = [meteo.ground_height(w.run["levels"], z, ps)
                  for z, ps in zip(prof["height"], prof["surface_pressure_hpa"])]
            zs = sorted(z for z in zs if z is not None)
            elev = zs[len(zs) // 2] if zs else None
        label = cfg.points[w.source.point].label or w.source.point
        if tuple(w.source.offset) != (0, 0):
            label += f" ({w.source.offset[0]:+d},{w.source.offset[1]:+d})"
        info = SourceInfo(role, _label(cfg, w.source.model), w.init, label,
                          pt["grid_lat"], pt["grid_lon"], elev, _hours_str(w))
        for s in sources:   # merge identical lines ("Freezing level + precipitation")
            if (s.model_label, s.run, s.point_label, s.hours) == (info.model_label, info.run, info.point_label, info.hours):
                s.role = f"{s.role} + {role.lower()}"
                return
        sources.append(info)

    # ---------------------------------------------------------------- freezing level
    fl_windows = plan_windows(cfg, chart.freezing_level, runs, "profile", start, end)
    fl_segments = []
    for w in fl_windows:
        prof = w.run["profile"]
        d = prof["data"][w.pkey]
        levels = w.run["levels"]
        times, fl, wbz = [], [], []
        has_depr = "dewpoint_depression" in d and chart.show_wetbulb_zero
        for k, h in enumerate(prof["hours"]):
            t = w.init + timedelta(hours=h)
            if t < w.start or t > w.end:
                continue
            _, f, wb = meteo.profile_diagnostics(
                levels, d["temperature"][k], d["height"][k],
                d["dewpoint_depression"][k] if has_depr else None,
                d["surface_pressure_hpa"][k], chart.freezing_level_method, chart.below_ground)
            times.append(t)
            fl.append(f)
            wbz.append(wb)
        if times:
            fl_segments.append(FreezingLevelSegment(
                w.source.model, _label(cfg, w.source.model), w.init,
                cfg.points[w.source.point].label or w.source.point, times, fl,
                wbz if has_depr else None))
            add_source("Freezing level", w)

    # ---------------------------------------------------------------- precipitation
    bar_h = chart.precip_bar_hours
    p_windows = plan_windows(cfg, chart.precip_sources, runs, "precip", start, end)
    bars: list[PrecipBar] = []
    b0 = _floor(start, bar_h)
    while b0 < end:
        b1 = b0 + timedelta(hours=bar_h)
        for idx, w in enumerate(p_windows):
            if not (w.start <= b0 < w.end or (idx == 0 and b0 < w.start < b1)):
                continue
            mm = _accum_between(w, b0, b1)
            if mm is None:   # e.g. bar extends past this run's data -> try the next window
                continue
            bars.append(PrecipBar(b0, b1, mm, w.source.model, _label(cfg, w.source.model)))
            break
        b0 = b1
    for w in p_windows:
        add_source("Precipitation", w)

    totals = _totals(chart, bars, start, end, tz)

    # ---------------------------------------------------------------- wind
    wind: list[WindSample] = []
    if chart.wind.enabled:
        wsrc = chart.freezing_level if chart.wind.source == "freezing_level" else chart.precip_sources
        for w in plan_windows(cfg, wsrc, runs, "profile", start, end):
            prof = w.run["profile"]
            d = prof["data"][w.pkey]
            if "wind_speed" not in d:
                continue
            for k, h in enumerate(prof["hours"]):
                t = w.init + timedelta(hours=h)
                if t < w.start or t > w.end or t.hour % chart.wind.every_hours:
                    continue
                spd, dirn = meteo.wind_at_height(w.run["levels"], d["height"][k], d["wind_speed"][k],
                                                 d["wind_direction"][k], chart.wind.elevation_m,
                                                 d["surface_pressure_hpa"][k])
                if spd is not None:
                    wind.append(WindSample(t, spd, dirn, w.source.model))

    fl_models = "→".join(dict.fromkeys(s.model_label for s in fl_segments)) or "no data"
    pr_models = "→".join(dict.fromkeys(b.model_label for b in bars)) or "no data"
    fmt_vars = {"fl_models": fl_models, "precip_models": pr_models}
    st = chart.style
    style = ChartStyle(**st.model_dump(), show_wetbulb_zero=chart.show_wetbulb_zero)
    return ChartData(
        chart_id=chart.id,
        title=_safe_format(chart.title, fmt_vars),
        subtitle=_safe_format(chart.subtitle, fmt_vars),
        timezone=tz, start=start, end=end, generated=now,
        freezing_level=fl_segments, precip_bars=bars, precip_totals=totals,
        precip_bar_hours=bar_h, precip_total_hours=chart.precip_total_hours, wind=wind,
        wind_elevation_m=chart.wind.elevation_m if chart.wind.enabled else None,
        reference_lines=[ReferenceLine(r.elevation, r.label, r.color, r.style) for r in chart.reference_lines],
        sources=sources, style=style)


def _safe_format(text: str, values: dict) -> str:
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        return text


def _accum_between(w: Window, b0: datetime, b1: datetime) -> Optional[float]:
    pr = w.run["precip"]
    series = dict(zip(pr["hours"], pr["data"][w.pkey]))
    h0 = max(0, int((b0 - w.init).total_seconds() // 3600))
    h1 = int((b1 - w.init).total_seconds() // 3600)
    if h1 <= 0:
        return None
    a0, a1 = series.get(h0), series.get(h1)
    if a0 is None or a1 is None:
        return None
    return max(0.0, a1 - a0)


def _totals(chart: ChartConfig, bars: list[PrecipBar], start, end, tz: str) -> list[PrecipTotal]:
    if not bars:
        return []
    zone = ZoneInfo(tz)
    bar_h, tot_h = chart.precip_bar_hours, chart.precip_total_hours
    # local period start, snapped to the nearest bar edge
    local = start.astimezone(zone).replace(hour=chart.precip_total_start_hour_local, minute=0,
                                           second=0, microsecond=0) - timedelta(days=1)
    p0 = local.astimezone(UTC)
    snapped = _floor(p0 + timedelta(hours=bar_h / 2), bar_h)
    p0 = snapped
    by_start = {b.start: b for b in bars}
    out = []
    while p0 < end:
        p1 = p0 + timedelta(hours=tot_h)
        slots = [p0 + timedelta(hours=bar_h * k) for k in range(tot_h // bar_h)]
        present = [by_start[s] for s in slots if s in by_start]
        if present and p1 > start:
            out.append(PrecipTotal(p0, p1, sum(b.mm for b in present), len(present) == len(slots)))
        p0 = p1
    return out
