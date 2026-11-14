"""Blended Snow Forecast: stored model runs + a SnowChartConfig -> SnowChartData.

The spread ("margin of error") is built only from data the service already has;
nothing is trained or calibrated against observations:

* lagged ensemble   - the latest ``blend.lagged_runs`` runs of each model
                      (older runs weighted 0.85**k),
* neighbourhood     - the model grid cells around the point; a cell whose terrain
                      is close to the panel elevation counts more, distant terrain
                      still adds spread (weight >= 0.25),
* physics           - fixed perturbations of the wet-bulb temperature used for the
                      rain/snow decision (+/- ``temperature_sigma_c``) and of the
                      snow-to-liquid ratio (x (1 +/- ``ratio_spread``)).

Every (run, cell, perturbation) is a member.  Per bar and elevation the chart
shows the weighted median and the weighted 10-90 % range, widened by
``blend.widen`` because such ensembles are under-dispersive.

Time windows / model hand-over follow the meteogram (assemble.build_chart_data):
the configured source chain is planned on the *latest* run of each model, and
each window's member runs are the recent runs of that model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from . import meteo, snow
from .assemble import Window, _hours_str, _label, _safe_format, period_origin, plan_windows
from .chartdata import (ElevationPanel, LevelBand, SnowBar, SnowChartData, SnowChartStyle, SnowTotal,
                        SourceInfo, Transition)
from .config import AppConfig, SnowChartConfig
from .datamart import parse_run_id

UTC = timezone.utc

RUN_DECAY = 0.85              # weight of the k-th newest run = RUN_DECAY ** k
CELL_TERRAIN_SCALE_M = 600.0  # cell weight = exp(-((z_cell - z) / scale)^2) ...
MIN_CELL_WEIGHT = 0.25        # ... but never below this
Q_LO, Q_MID, Q_HI = 0.10, 0.50, 0.90
BAR_FLOOR_FRAC = 0.20         # spread never narrower than +/- 20 % of the centre
TOTAL_SNOW_FLOOR_CM = 2.0     # ... and snow totals never narrower than +/- 2 cm


# =========================================================================== input parsing
def _nan(v) -> float:
    return np.nan if v is None else float(v)


def _grid(rows, n_h: int, n_l: int) -> np.ndarray:
    """[hour][level] nested list (None anywhere, also whole rows) -> float array with NaN."""
    out = np.full((n_h, n_l), np.nan)
    for k, row in enumerate((rows or [])[:n_h]):
        if row is None:
            continue
        vals = [_nan(v) for v in row[:n_l]]
        out[k, :len(vals)] = vals
    return out


def _series(vals, n: int) -> np.ndarray:
    out = np.full(n, np.nan)
    vals = [_nan(v) for v in (vals or [])[:n]]
    out[:len(vals)] = vals
    return out


def _as_list(row: np.ndarray) -> list:
    return [None if not np.isfinite(v) else float(v) for v in row]


@dataclass
class _Column:
    """One (run, grid cell) of the blend, pre-evaluated at the chart's elevations."""
    run_index: int               # 0 = newest run of the model
    init: datetime
    cell: tuple[int, int]        # (dj, di) relative to the point's cell
    precip: dict[int, float]     # forecast hour -> accumulated mm
    prof_hours: np.ndarray       # [H]
    ground_m: float              # median model terrain height (NaN if unknown)
    tw: np.ndarray               # [E, 3, H] wet-bulb °C at z, z + base, z + 2*base
    t: np.ndarray                # [E, H] air temperature °C at z
    wind: Optional[np.ndarray]   # [E, H] wind speed km/h at z (None = not used / unavailable)
    wbz: np.ndarray              # [H] wet-bulb 0 °C height (m), NaN = undefined
    fl: np.ndarray               # [H] freezing level (m), NaN = undefined


def _zero_height(z: np.ndarray, v: np.ndarray) -> float:
    """0 °C crossing with the meteogram's "highest" method and "extrapolate" below-ground
    policy (same as meteo.profile_diagnostics(..., "highest", "extrapolate"))."""
    ok = np.isfinite(z) & np.isfinite(v)
    if ok.sum() < 2:
        return np.nan
    z, v = z[ok], v[ok]
    h = meteo.zero_crossing_height(z, v, float(np.min(z)), "highest")
    return np.nan if h is None else h


def _make_column(run: dict, run_index: int, cell: tuple[int, int], prof: dict, precip: list,
                 elevs: np.ndarray, base_m: float, use_wind: bool) -> _Column:
    levels = list(run["levels"])
    hours = list(run["profile"]["hours"])
    n_h, n_l = len(hours), len(levels)
    t = _grid(prof.get("temperature"), n_h, n_l)
    z = _grid(prof.get("height"), n_h, n_l)
    if "dewpoint_depression" in prof:
        depr = _grid(prof.get("dewpoint_depression"), n_h, n_l)
    else:           # config validation requires it; fall back to Tw = T (under-forecasts snow)
        depr = np.zeros_like(t)
    p = np.broadcast_to(np.asarray(levels, dtype=float), (n_h, n_l))
    with np.errstate(invalid="ignore", over="ignore"):
        tw = meteo.wet_bulb(t, depr, p)
    psfc = _series(prof.get("surface_pressure_hpa"), n_h)
    wspd = _grid(prof.get("wind_speed"), n_h, n_l) if use_wind and "wind_speed" in prof else None

    n_e = len(elevs)
    zq = np.concatenate([elevs, elevs + base_m, elevs + 2 * base_m])    # [3E]
    tw_e = np.full((n_e, 3, n_h), np.nan)
    t_e = np.full((n_e, n_h), np.nan)
    w_e = np.full((n_e, n_h), np.nan) if wspd is not None else None
    wbz, fl, ground = np.full(n_h, np.nan), np.full(n_h, np.nan), []
    for k in range(n_h):
        # all levels incl. the ones under the model terrain ("extrapolate" policy)
        tw_e[:, :, k] = snow.value_at_height(z[k], tw[k], zq).reshape(3, n_e).T
        t_e[:, k] = snow.value_at_height(z[k], t[k], elevs)
        if w_e is not None:
            w_e[:, k] = snow.value_at_height(z[k], wspd[k], elevs)
        wbz[k] = _zero_height(z[k], tw[k])
        fl[k] = _zero_height(z[k], t[k])
        g = meteo.ground_height(levels, _as_list(z[k]), None if not np.isfinite(psfc[k]) else float(psfc[k]))
        if g is not None and np.isfinite(g):
            ground.append(g)
    pr_hours = run["precip"]["hours"]
    acc = {int(h): float(v) for h, v in zip(pr_hours, precip or []) if v is not None}
    return _Column(run_index, parse_run_id(run["run"]), cell, acc, np.asarray(hours, dtype=float),
                   float(np.median(ground)) if ground else np.nan, tw_e, t_e, w_e, wbz, fl)


def _cells_of(run: dict, pk: str, neighbourhood_km: float) -> list[tuple[tuple[int, int], dict, list]]:
    """(cell, profile dict, precip series) for the point's cell and its stored neighbours
    within the chart's neighbourhood (square block of round(km / spacing) cells, like the extractor)."""
    prof, pr = run.get("profile") or {}, run.get("precip") or {}
    centre_p, centre_r = (prof.get("data") or {}).get(pk), (pr.get("data") or {}).get(pk)
    if centre_p is None or centre_r is None:
        return []
    out = [((0, 0), centre_p, centre_r)]
    if neighbourhood_km <= 0:
        return out
    spacing = (run.get("points") or {}).get(pk, {}).get("spacing_km")
    r_max = int(round(neighbourhood_km / spacing)) if spacing else None
    nb_p = (prof.get("neighbours") or {}).get(pk) or {}
    nb_r = (pr.get("neighbours") or {}).get(pk) or {}
    for key in sorted(nb_p):
        if key not in nb_r:
            continue
        try:
            dj, di = (int(x) for x in key.split(","))
        except ValueError:
            continue
        if (dj, di) == (0, 0) or (r_max is not None and max(abs(dj), abs(di)) > r_max):
            continue
        out.append(((dj, di), nb_p[key], nb_r[key]))
    return out


def _member_runs(runs: list[dict], pk: str, n: int) -> list[tuple[int, dict]]:
    """(index, run) of the recent runs usable for a window: same grid signature as the
    newest run (so cells match) and containing the point in both layers."""
    if not runs:
        return []
    sig = runs[0].get("signature")
    out = []
    for k, r in enumerate(runs[:n]):
        if r.get("signature") != sig:
            continue
        if pk in ((r.get("precip") or {}).get("data") or {}) and pk in ((r.get("profile") or {}).get("data") or {}):
            out.append((k, r))
    return out


# =========================================================================== statistics
def _row_stats(vals: np.ndarray, w: np.ndarray):
    """Weighted median / 10th / 90th percentile per row of ``vals`` [n, M]."""
    out = np.full((vals.shape[0], 3), np.nan)
    for i in range(vals.shape[0]):
        out[i] = snow.weighted_quantiles(vals[i], w, [Q_MID, Q_LO, Q_HI])
    return out[:, 0], out[:, 1], out[:, 2]


def _widen(c: float, lo: float, hi: float, widen: float, floor_abs: float = 0.0):
    """Widen the raw 10-90 % range around the centre and apply the minimum spread."""
    lo2 = max(0.0, c - widen * (c - lo))
    hi2 = c + widen * (hi - c)
    if c > 0:
        d = max(BAR_FLOOR_FRAC * c, floor_abs)
        hi2 = max(hi2, c + d)
        lo2 = min(lo2, max(0.0, c - d))
    return float(lo2), float(hi2)


@dataclass
class _ElevBlend:
    snow: np.ndarray     # [n_slots, M] cm
    rain: np.ndarray     # [n_slots, M] mm
    frac: np.ndarray     # [n_slots, M] snow fraction
    mm: np.ndarray       # [n_slots, M] precipitation
    w: np.ndarray        # [M]


@dataclass
class _WindowBlend:
    columns: list[_Column]
    mm: np.ndarray                   # [n_slots, C] (coverage check)
    elev: list[_ElevBlend]
    n_runs: int
    n_cells: int                     # cells of the newest member run
    radius: int
    spacing_km: Optional[float]


def _accum(col: _Column, b0: datetime, b1: datetime) -> float:
    """Precipitation (mm) in [b0, b1] for one column; NaN if not covered (as assemble._accum_between)."""
    h0 = max(0, int((b0 - col.init).total_seconds() // 3600))
    h1 = int((b1 - col.init).total_seconds() // 3600)
    if h1 <= 0:
        return np.nan
    a0, a1 = col.precip.get(h0), col.precip.get(h1)
    if a0 is None or a1 is None:
        return np.nan
    return max(0.0, a1 - a0)


def _tinterp(x: np.ndarray, y: np.ndarray, xq: np.ndarray) -> np.ndarray:
    ok = np.isfinite(y)
    if not ok.any():
        return np.full(xq.shape, np.nan)
    return np.interp(xq, x[ok], y[ok])      # clamps outside the profile hours


def _blend_window(cfg: AppConfig, chart: SnowChartConfig, w: Window, model_runs: list[dict],
                  slots: list[datetime], elevs: np.ndarray) -> Optional[_WindowBlend]:
    b = chart.blend
    members = _member_runs(model_runs, w.pkey, b.lagged_runs)
    cols: list[_Column] = []
    n_cells, spacing, radius = 0, None, 0
    for k, run in members:
        cells = _cells_of(run, w.pkey, b.neighbourhood_km)
        if not n_cells:
            n_cells = len(cells)
            spacing = (run.get("points") or {}).get(w.pkey, {}).get("spacing_km")
            radius = max([max(abs(c[0][0]), abs(c[0][1])) for c in cells], default=0)
        for cell, prof, pr in cells:
            cols.append(_make_column(run, k, cell, prof, pr, elevs, b.snow_level_offset_m, b.use_wind))
    if not cols or not slots:
        return None

    bar = timedelta(hours=chart.bar_hours)
    n_s, n_c = len(slots), len(cols)
    mm = np.array([[_accum(c, s0, s0 + bar) for c in cols] for s0 in slots])               # [S, C]
    mid = np.array([[(s0 + bar / 2 - c.init).total_seconds() / 3600 for c in cols] for s0 in slots])
    base = b.snow_level_offset_m
    off = snow.snow_level_offset_m(mm / chart.bar_hours, base)
    frac_off = (np.asarray(off) - base) / base if base > 0 else np.zeros_like(mm)        # 0..1

    d_tw = np.unique([-b.temperature_sigma_c, 0.0, b.temperature_sigma_c])               # [P]
    f_ratio = np.unique([1 - b.ratio_spread, 1.0, 1 + b.ratio_spread])                   # [Q]
    n_p, n_q = len(d_tw), len(f_ratio)
    run_w = np.array([RUN_DECAY ** c.run_index for c in cols])
    zg = np.array([c.ground_m for c in cols])

    elev_out = []
    for e, z in enumerate(elevs):
        tw_a = np.empty((n_s, n_c))
        tw_b = np.empty((n_s, n_c))
        t_z = np.empty((n_s, n_c))
        wind = np.full((n_s, n_c), np.nan)
        for j, c in enumerate(cols):
            tw_a[:, j] = _tinterp(c.prof_hours, c.tw[e, 1], mid[:, j])
            tw_b[:, j] = _tinterp(c.prof_hours, c.tw[e, 2], mid[:, j])
            t_z[:, j] = _tinterp(c.prof_hours, c.t[e], mid[:, j])
            if c.wind is not None:
                wind[:, j] = _tinterp(c.prof_hours, c.wind[e], mid[:, j])
        # Minder offset: evaluate the wet-bulb profile base..2*base above the elevation
        # (linear between the two pre-computed heights; exact unless a model level lies between).
        tw_eff = tw_a + frac_off * (tw_b - tw_a)
        # members [S, C, P, Q]
        f = snow.snow_fraction(tw_eff[:, :, None, None] + d_tw[None, None, :, None])
        f = np.broadcast_to(f, (n_s, n_c, n_p, n_q))
        use_wind = b.use_wind and np.isfinite(wind).any()
        ratio = snow.snow_ratio(t_z[:, :, None, None] + d_tw[None, None, :, None],
                                wind[:, :, None, None] if use_wind else None) * f_ratio[None, None, None, :]
        m4 = np.broadcast_to(mm[:, :, None, None], (n_s, n_c, n_p, n_q))
        snow_cm = m4 * f * ratio / 10.0
        rain_mm = m4 * (1.0 - f)
        cell_w = np.maximum(MIN_CELL_WEIGHT, np.exp(-((zg - z) / CELL_TERRAIN_SCALE_M) ** 2))
        cell_w = np.where(np.isfinite(cell_w), cell_w, MIN_CELL_WEIGHT)
        wts = np.repeat(cell_w * run_w, n_p * n_q)
        m = n_c * n_p * n_q
        elev_out.append(_ElevBlend(snow_cm.reshape(n_s, m), rain_mm.reshape(n_s, m),
                                   np.ascontiguousarray(f).reshape(n_s, m), m4.reshape(n_s, m), wts))
    return _WindowBlend(cols, mm, elev_out, len(members), n_cells, radius, spacing)


# =========================================================================== main entry
def build_snow_chart_data(cfg: AppConfig, chart: SnowChartConfig, runs: dict[str, list[dict]],
                          now: Optional[datetime] = None) -> SnowChartData:
    """Build the Blended Snow Forecast.  ``runs[model]`` lists stored runs newest first."""
    now = now or datetime.now(UTC)
    tz = cfg.general.timezone
    b = chart.blend
    runs = {m: [r for r in (lst or []) if r] for m, lst in (runs or {}).items()}
    latest = {m: lst[0] for m, lst in runs.items() if lst}
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=chart.history_hours)
    end = start + timedelta(hours=chart.hours + chart.history_hours)
    if chart.start_at_run:
        # the chart covers the first available source's run from its start (12Z run -> 12Z to 12Z + hours)
        first = next((latest[s.model] for s in chart.sources if s.model in latest), None)
        if first is not None:
            start = parse_run_id(first["run"])
            end = start + timedelta(hours=chart.hours)

    windows = plan_windows(cfg, chart.sources, latest, "precip", start, end)
    origin = period_origin(cfg, chart.sources, start, tz, chart.total_start_hour_local, chart.style.tick_hour_utc)
    bar_h = chart.bar_hours
    bar = timedelta(hours=bar_h)
    slots = []
    s0 = origin + (start - origin) // bar * bar
    while s0 < end:
        slots.append(s0)
        s0 += bar

    elevs = np.array([e.elevation for e in chart.elevations], dtype=float)
    blends = [_blend_window(cfg, chart, w, runs.get(w.source.model, []), slots, elevs) for w in windows]

    # ---------------------------------------------------------------- bars: slot -> window
    chosen: dict[int, int] = {}
    for i, b0 in enumerate(slots):
        b1 = b0 + bar
        for idx, w in enumerate(windows):
            if not (w.start <= b0 < w.end or (idx == 0 and b0 < w.start < b1)):
                continue
            wb = blends[idx]
            if wb is None or not np.isfinite(wb.mm[i]).any():   # past this run's data -> next window
                continue
            chosen[i] = idx
            break

    panels: list[ElevationPanel] = []
    present: list[dict[int, int]] = []          # per elevation: slot -> window idx
    for e, ec in enumerate(chart.elevations):
        panel = ElevationPanel(ec.elevation, ec.label)
        pres: dict[int, int] = {}
        stats = {}
        for idx, wb in enumerate(blends):
            if wb is None:
                continue
            eb = wb.elev[e]
            stats[idx] = (_row_stats(eb.snow, eb.w), _row_stats(eb.rain, eb.w))
        for i, idx in sorted(chosen.items()):
            eb = blends[idx].elev[e]
            (sc, sl, sh), (rc, rl, rh) = stats[idx]
            if not (np.isfinite(sc[i]) and np.isfinite(rc[i])):
                continue
            s_lo, s_hi = _widen(sc[i], sl[i], sh[i], b.widen)
            r_lo, r_hi = _widen(rc[i], rl[i], rh[i], b.widen)
            fr, wet = eb.frac[i], eb.mm[i] > 0
            p = snow.weighted_mean(fr[wet], eb.w[wet]) if np.any(wet & np.isfinite(fr)) else None
            if p is None:
                p = snow.weighted_mean(fr, eb.w)
            p = 0.0 if p is None else p
            w = windows[idx]
            panel.bars.append(SnowBar(
                slots[i], slots[i] + bar, float(sc[i]), s_lo, s_hi, float(rc[i]), r_lo, r_hi, p,
                bool(0.2 < p < 0.8 and (sc[i] + rc[i]) > 0), w.source.model, _label(cfg, w.source.model)))
            pres[i] = idx
        panels.append(panel)
        present.append(pres)

    # ---------------------------------------------------------------- snow level band
    lb_times, lb = [], {"sl": [], "lo": [], "hi": [], "fl": []}
    for idx, (w, wb) in enumerate(zip(windows, blends)):
        if wb is None:
            continue
        for h in w.run["profile"]["hours"]:
            t = w.init + timedelta(hours=h)
            if t < max(w.start, start) or t > min(w.end, end) or (lb_times and t <= lb_times[-1]):
                continue
            sl_v, fl_v, wt = [], [], []
            for c in wb.columns:
                hc = (t - c.init).total_seconds() / 3600
                k = np.flatnonzero(np.isclose(c.prof_hours, hc))
                if not len(k):
                    continue
                sl_v.append(c.wbz[k[0]] - b.snow_level_offset_m)
                fl_v.append(c.fl[k[0]])
                wt.append(RUN_DECAY ** c.run_index)
            if not wt:
                continue
            c_, lo_, hi_ = snow.weighted_quantiles(sl_v, wt, [Q_MID, Q_LO, Q_HI])
            f_ = snow.weighted_quantiles(fl_v, wt, Q_MID)
            ok = np.isfinite(c_)
            lb_times.append(t)
            lb["sl"].append(float(c_) if ok else None)
            lb["lo"].append(float(c_ - b.widen * (c_ - lo_)) if ok else None)
            lb["hi"].append(float(c_ + b.widen * (hi_ - c_)) if ok else None)
            lb["fl"].append(float(f_) if np.isfinite(f_) else None)
    level_band = LevelBand(lb_times, lb["sl"], lb["lo"], lb["hi"], lb["fl"]) if lb_times else None

    # ---------------------------------------------------------------- trim + totals
    if chart.trim_to_data:
        data_end = max([bb.end for p in panels for bb in p.bars] + lb_times, default=None)
        if data_end is not None and data_end > start:
            end = min(end, data_end)
    slot_index = {s: i for i, s in enumerate(slots)}
    for e, panel in enumerate(panels):
        panel.totals = _totals(chart, blends, present[e], e, slots, slot_index, origin, start, end)

    # ---------------------------------------------------------------- provenance
    sources, transitions, clauses = [], [], []
    for idx, (w, wb) in enumerate(zip(windows, blends)):
        if idx and windows[idx - 1].source.model != w.source.model:
            transitions.append(Transition(w.start, f"{_label(cfg, windows[idx - 1].source.model)}→"
                                                   f"{_label(cfg, w.source.model)}"))
        if wb is None:
            continue
        cells = f"{wb.n_cells} cell{'s' if wb.n_cells != 1 else ''}"
        role = f"Snow blend ({wb.n_runs} run{'s' if wb.n_runs != 1 else ''} × {cells})"
        pt = w.run["points"][w.pkey]
        label = cfg.points[w.source.point].label or w.source.point
        if tuple(w.source.offset) != (0, 0):
            label += f" ({w.source.offset[0]:+d},{w.source.offset[1]:+d})"
        centre = next((c for c in wb.columns if c.run_index == wb.columns[0].run_index and c.cell == (0, 0)), None)
        elev = centre.ground_m if centre is not None and np.isfinite(centre.ground_m) else None
        sources.append(SourceInfo(role, _label(cfg, w.source.model), w.init, label,
                                  pt.get("grid_lat"), pt.get("grid_lon"), elev, _hours_str(w)))
        clause = f"{_label(cfg, w.source.model)} {wb.n_runs} run{'s' if wb.n_runs != 1 else ''} × {cells}"
        if wb.radius:
            clause += f" ({2 * wb.radius + 1}×{2 * wb.radius + 1}"
            clause += f", {round(float(wb.spacing_km), 1):g} km)" if wb.spacing_km else ")"
        if clause not in clauses:
            clauses.append(clause)
    recipe = ""
    if clauses:
        n_pert = len(np.unique([-b.temperature_sigma_c, 0, b.temperature_sigma_c])) * \
            len(np.unique([1 - b.ratio_spread, 1, 1 + b.ratio_spread]))
        base = b.snow_level_offset_m
        recipe = (f"Blend: {'; '.join(clauses)} × {n_pert} phase/ratio perturbations"
                  f" · spread 10–90 % × {b.widen:g}"
                  f" · snow level = wet-bulb 0 °C − {base:g}–{2 * base:g} m")

    models = "→".join(dict.fromkeys(bb.model_label for p in panels for bb in p.bars)) or "no data"
    fmt = {"models": models}
    return SnowChartData(
        chart_id=chart.id,
        title=_safe_format(chart.title, fmt),
        subtitle=_safe_format(chart.subtitle, fmt),
        timezone=tz, start=start, end=end, generated=now,
        panels=panels, level_band=level_band, bar_hours=bar_h, total_hours=chart.total_hours,
        transitions=transitions, sources=sources, recipe=recipe,
        style=SnowChartStyle(**chart.style.model_dump()))


def _totals(chart: SnowChartConfig, blends: list[Optional[_WindowBlend]], present: dict[int, int], e: int,
            slots: list[datetime], slot_index: dict[datetime, int], origin: datetime,
            start: datetime, end: datetime) -> list[SnowTotal]:
    """Period totals summed per member.  Within one window only members covering every
    present bar are used (the newest run always does).  A period spanning a model
    hand-over adds the per-window quantiles (i.e. assumes the windows are fully
    correlated, which is exact for the median and conservative for the range)."""
    if not present:
        return []
    bar_h, tot_h, b = chart.bar_hours, chart.total_hours, chart.blend
    period = timedelta(hours=tot_h)
    p0 = origin + (start - origin) // period * period
    out = []
    while p0 < end:
        p1 = p0 + period
        pslots = [p0 + timedelta(hours=bar_h * k) for k in range(tot_h // bar_h)]
        rows = [slot_index[s] for s in pslots if s in slot_index and slot_index[s] in present]
        if rows and p1 > start:
            acc = np.zeros(6)       # snow c/lo/hi, rain c/lo/hi (raw)
            for idx in dict.fromkeys(present[i] for i in rows):
                eb = blends[idx].elev[e]
                r = [i for i in rows if present[i] == idx]
                s_m, r_m = eb.snow[r], eb.rain[r]
                full = np.all(np.isfinite(s_m) & np.isfinite(r_m), axis=0)
                if full.any():
                    ws = np.where(full, eb.w, 0.0)
                    acc[:3] += snow.weighted_quantiles(s_m.sum(axis=0), ws, [Q_MID, Q_LO, Q_HI])
                    acc[3:] += snow.weighted_quantiles(r_m.sum(axis=0), ws, [Q_MID, Q_LO, Q_HI])
                else:   # no member covers every bar: add the per-bar quantiles
                    for i in r:
                        acc[:3] += snow.weighted_quantiles(eb.snow[i], eb.w, [Q_MID, Q_LO, Q_HI])
                        acc[3:] += snow.weighted_quantiles(eb.rain[i], eb.w, [Q_MID, Q_LO, Q_HI])
            if np.all(np.isfinite(acc)):
                s_lo, s_hi = _widen(acc[0], acc[1], acc[2], b.widen, TOTAL_SNOW_FLOOR_CM)
                r_lo, r_hi = _widen(acc[3], acc[4], acc[5], b.widen)
                out.append(SnowTotal(p0, p1, float(acc[0]), s_lo, s_hi, float(acc[3]), r_lo, r_hi,
                                     len(rows) == len(pslots)))
        p0 = p1
    return out
