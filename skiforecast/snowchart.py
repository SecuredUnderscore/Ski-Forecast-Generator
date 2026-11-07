"""Render the Blended Snow Forecast PNG (snow level band + per-elevation snow/rain bars)."""
from __future__ import annotations

import math
import pathlib
from typing import Optional
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.legend_handler import HandlerBase  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from .chart import _fmt_mm, _hour_label, _tick_label, _tick_step, _time_ticks, _tz_note, format_zulu_local  # noqa: E402
from .chartdata import SnowChartData  # noqa: E402

_REF_COLOR = "#4a6b8a"
_GREY = "#555555"
_NICE_STEPS = (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500)
_MIN_BAR = 0.2          # bars at or below this upper bound (cm / mm) are not drawn
_MIN_LABEL = 0.3        # central values below this are not labelled


def _num(v) -> float:
    """float or NaN for None / garbage."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return math.nan
    return f


def _nan(values) -> np.ndarray:
    return np.array([_num(v) for v in values], dtype=float)


def _nice_step(vmax: float, max_ticks: int = 4) -> float:
    return next((s for s in _NICE_STEPS if vmax / s <= max_ticks), _NICE_STEPS[-1])


def _fmt(v: float) -> str:
    """chart._fmt_mm, but values that round to 1 print as "1" rather than "1.0"."""
    return _fmt_mm(v) if v < 0.95 else f"{v:.0f}"


def _whole(v: float) -> str:
    return "?" if math.isnan(v) else f"{v:.0f}"


class _WhiskerHandler(HandlerBase):
    """Legend glyph: a vertical whisker with caps."""

    def create_artists(self, legend, orig_handle, xdescent, ydescent, width, height, fontsize, trans):
        cx = width / 2 - xdescent
        y0, y1 = -ydescent - height * 0.15, -ydescent + height * 1.15
        cap = width * 0.18
        color = orig_handle.get_color()
        arts = [Line2D([cx, cx], [y0, y1]), Line2D([cx - cap, cx + cap], [y0, y0]),
                Line2D([cx - cap, cx + cap], [y1, y1])]
        for a in arts:
            a.set_color(color)
            a.set_linewidth(1.2)
            a.set_transform(trans)
        return arts


def _style_time_axis(ax, st, days, within, bg):
    ax.set_facecolor(bg)
    ax.grid(True, axis="x", color="white", linewidth=1.0, alpha=0.9)
    ax.grid(True, axis="y", color="white", linewidth=0.6, alpha=0.6)
    ax.set_xticks(mdates.date2num(days))
    ax.set_xticklabels([_tick_label(t) for t in days])
    if within:
        ax.set_xticks(mdates.date2num(within), minor=True)
        ax.set_xticklabels([_hour_label(t) for t in within], minor=True)
        ax.tick_params(axis="x", which="minor", labelsize=8.5, labelcolor="#444444", length=3)
        ax.grid(True, axis="x", which="minor", color="white", linewidth=0.6, alpha=0.6)
    ax.tick_params(axis="x", which="major", labelsize=10.5)
    ax.set_axisbelow(True)
    for side in ("top", "left", "bottom", "right"):
        ax.spines[side].set_color(_GREY)


def _vlines(ax, data, x0, x1, label_top: bool):
    st = data.style
    if st.show_model_transitions:
        for tr in data.transitions:
            tx = mdates.date2num(tr.time)
            if x0 < tx < x1:
                ax.axvline(tx, color="#777777", linestyle=":", linewidth=1.2, zorder=3)
                if label_top and tr.label:
                    ax.text(tx, 1.004, tr.label, transform=ax.get_xaxis_transform(), ha="center",
                            va="bottom", fontsize=8.5, color=_GREY, zorder=6)
    if st.show_now_line:
        nx = mdates.date2num(data.generated)
        if x0 < nx < x1:
            ax.axvline(nx, color="#333333", linewidth=1.0, alpha=0.6, zorder=3)


def _darker(color: str, f: float = 0.75):
    r, g, b = matplotlib.colors.to_rgb(color)
    return (r * f, g * f, b * f)


def _draw_level_panel(ax, data, x0, x1):
    st = data.style
    elevs = [p.elevation_m for p in data.panels if not math.isnan(_num(p.elevation_m))]
    band = data.level_band
    xs = sl = lo = hi = fl = None
    if band is not None and band.times:
        n = len(band.times)
        xs = mdates.date2num(band.times)

        def arr(vals):
            a = _nan(vals or [])
            return a[:n] if len(a) >= n else np.concatenate([a, np.full(n - len(a), np.nan)])
        sl, lo, hi, fl = arr(band.snow_level), arr(band.snow_level_lo), arr(band.snow_level_hi), \
            arr(band.freezing_level)

    def finite_max(*arrays):
        vals = [float(np.nanmax(a)) for a in arrays if a is not None and np.isfinite(a).any()]
        return max(vals) if vals else None

    top = max(elevs + [v for v in [finite_max(hi, sl)] if v is not None], default=None)
    ymin = max(0.0, math.floor((min(elevs) - 600) / 500) * 500) if elevs else 0.0
    ymax = math.ceil((top + 500) / 500) * 500 if top is not None else 3000.0
    if ymax <= ymin:
        ymax = ymin + 1000
    ax.set_ylim(ymin, ymax)
    ax.set_xlim(x0, x1)

    handles = []
    if xs is not None:
        if np.isfinite(lo).any() and np.isfinite(hi).any():
            ok = np.isfinite(lo) & np.isfinite(hi)
            ax.fill_between(xs, lo, hi, where=ok, color=st.snow_level_color, alpha=0.2, linewidth=0,
                            zorder=2, interpolate=False)
        if np.isfinite(sl).any():
            ax.plot(xs, sl, color=st.snow_level_color, linewidth=1.8, zorder=5)
            handles.append(Line2D([], [], color=st.snow_level_color, linewidth=1.8,
                                  label="Snow level (wet-bulb 0 °C − offset)"))
        if st.show_freezing_level and np.isfinite(fl).any():
            ax.plot(xs, fl, color=st.freezing_level_color, linewidth=1.1, zorder=4)
            handles.append(Line2D([], [], color=st.freezing_level_color, linewidth=1.1, label="Freezing level"))
    if not handles:
        handles.append(Line2D([], [], color=st.snow_level_color, linewidth=1.8, label="Snow level (no data)"))

    for p in data.panels:
        e = _num(p.elevation_m)
        if math.isnan(e) or not (ymin <= e <= ymax):
            continue
        ax.axhline(e, color=_REF_COLOR, linestyle="--", linewidth=1.1, zorder=3)
        lbl = f"{p.label} {e:.0f} m" if p.label else f"{e:.0f} m"
        ax.text(0.005, e, f" {lbl}", transform=ax.get_yaxis_transform(), ha="left", va="bottom",
                fontsize=8.5, color=_REF_COLOR, zorder=6)

    ax.yaxis.set_major_locator(matplotlib.ticker.MultipleLocator(500 if ymax - ymin <= 3500 else 1000))
    ax.tick_params(axis="y", labelsize=9.5)
    ax.set_ylabel("Snow level\nasl (m)", fontsize=10.5, fontweight="bold", rotation=0, ha="right",
                  va="center")
    ax.yaxis.set_label_coords(-0.04, 0.5)
    ax.legend(handles=handles, loc="upper right", fontsize=8.5, framealpha=0.9)


def _draw_elevation_panel(ax, axr, panel, data, x0, x1):
    st = data.style
    bars = panel.bars
    snow_hi = [_num(b.snow_hi) for b in bars] + [_num(b.snow_cm) for b in bars]
    rain_hi = [_num(b.rain_hi) for b in bars] + [_num(b.rain_mm) for b in bars]
    smax = st.snow_axis_max_cm or max([5.0] + [1.6 * v for v in snow_hi if math.isfinite(v)])
    rmax = st.rain_axis_max_mm or max([5.0] + [1.6 * v for v in rain_hi if math.isfinite(v)])
    ax.set_ylim(0, smax)
    axr.set_ylim(0, rmax)
    ax.set_xlim(x0, x1)

    # label font shrinks when slots get narrow (long-range charts)
    ax_w_in = ax.get_position().width * ax.figure.get_figwidth()
    span = max(x1 - x0, 1e-9)
    slot_days = np.median([mdates.date2num(b.end) - mdates.date2num(b.start) for b in bars]) if bars else 0.125
    half_pt = 0.44 * slot_days / span * ax_w_in * 72
    fs = float(min(8.0, max(5.5, half_pt / 2.0)))

    def draw(target, bx, bw, c, lo, hi, color, text_color, mixed, show_range):
        if not math.isfinite(hi):
            hi = c
        if not math.isfinite(hi) or hi <= _MIN_BAR:
            return
        c = c if math.isfinite(c) else hi
        lo = lo if math.isfinite(lo) else c
        kw = dict(hatch="///", edgecolor=(1, 1, 1, 0.8)) if mixed else dict(edgecolor="none")
        target.bar(bx, max(c, 0.0), width=bw, align="edge", color=color, alpha=0.8, linewidth=0, zorder=2, **kw)
        cx = bx + bw / 2
        dark = _darker(text_color, 0.85)
        if hi > lo:
            cap = bw * 0.18
            target.vlines(cx, lo, hi, color=dark, linewidth=1.0, alpha=0.8, zorder=4)
            target.hlines([lo, hi], cx - cap, cx + cap, color=dark, linewidth=1.0, alpha=0.8, zorder=4)
        if st.show_bar_labels and c >= _MIN_LABEL:
            off = 2
            if show_range:
                target.annotate(f"{_fmt(lo)}–{_fmt(hi)}", (cx, hi), xytext=(0, off),
                                textcoords="offset points", ha="center", va="bottom", fontsize=6.5 * fs / 8,
                                color=text_color, zorder=6, annotation_clip=False)
                off += 8
            target.annotate(_fmt(c), (cx, hi), xytext=(0, off), textcoords="offset points", ha="center",
                            va="bottom", fontsize=fs, fontweight="bold", color=text_color, zorder=6,
                            annotation_clip=False)

    if st.show_slot_separators:
        edges = sorted({mdates.date2num(b.start) for b in bars} | {mdates.date2num(b.end) for b in bars})
        for x in edges:
            if x0 < x < x1:
                ax.axvline(x, color=st.slot_separator_color, linewidth=0.7, alpha=0.9, zorder=1)

    for b in bars:
        bx = mdates.date2num(b.start)
        bw = mdates.date2num(b.end) - bx
        if not (math.isfinite(bx) and bw > 0) or bx + bw <= x0 or bx >= x1:
            continue
        draw(ax, bx + 0.04 * bw, 0.44 * bw, _num(b.snow_cm), _num(b.snow_lo), _num(b.snow_hi),
             st.snow_color, st.snow_text_color, b.mixed, st.show_bar_ranges)
        draw(axr, bx + 0.52 * bw, 0.44 * bw, _num(b.rain_mm), _num(b.rain_lo), _num(b.rain_hi),
             st.rain_color, st.rain_text_color, b.mixed, st.show_bar_ranges)

    # totals
    for tot in panel.totals:
        # drawn at the end of its period, like the meteogram (a period running past the
        # chart sits right-aligned on the right edge)
        cx = min(mdates.date2num(tot.end), x1)
        if not (x0 <= cx <= x1):
            continue
        ha = "right" if cx >= x1 else "center"
        ls = "-" if tot.complete else "--"
        sc, slo, shi = _num(tot.snow_cm), _num(tot.snow_lo), _num(tot.snow_hi)
        rc, rlo, rhi = _num(tot.rain_mm), _num(tot.rain_lo), _num(tot.rain_hi)
        show_snow = (math.isfinite(sc) and sc >= 0.5) or (math.isfinite(shi) and shi >= 1)
        show_rain = (math.isfinite(rc) and rc >= 1) or (math.isfinite(rhi) and rhi >= 2)
        if show_snow:
            ax.text(cx, 0.90, f"{_whole(sc)} cm ({_whole(slo)}–{_whole(shi)})", transform=ax.get_xaxis_transform(),
                    ha=ha, va="center", fontsize=9.5, fontweight="bold", color=st.snow_text_color, zorder=7,
                    bbox=dict(boxstyle="round,pad=0.25", facecolor="white", alpha=0.9, edgecolor=st.snow_text_color,
                              linestyle=ls, linewidth=1.1))
        if show_rain:   # a dry period shows no box at all; rain alone takes the top slot
            ax.text(cx, 0.72 if show_snow else 0.90, f"{_whole(rc)} mm ({_whole(rlo)}–{_whole(rhi)})",
                    transform=ax.get_xaxis_transform(), ha=ha, va="center", fontsize=9,
                    fontweight="bold", color=st.rain_text_color, zorder=7,
                    bbox=dict(boxstyle="round,pad=0.25", facecolor="white", alpha=0.9,
                              edgecolor=st.rain_text_color, linestyle=ls, linewidth=1.1))

    # axes
    ax.yaxis.set_major_locator(matplotlib.ticker.MultipleLocator(_nice_step(smax)))
    axr.yaxis.set_major_locator(matplotlib.ticker.MultipleLocator(_nice_step(rmax)))
    ax.set_ylim(0, smax)
    axr.set_ylim(0, rmax)
    ax.tick_params(axis="y", colors=st.snow_text_color, labelsize=9.5)
    axr.tick_params(axis="y", colors=st.rain_text_color, labelsize=9.5)
    ax.spines["left"].set_color(st.snow_text_color)
    axr.spines["left"].set_visible(False)
    axr.spines["top"].set_visible(False)
    axr.spines["bottom"].set_visible(False)
    axr.spines["right"].set_color(st.rain_text_color)
    ax.set_ylabel("cm\nsnow", color=st.snow_text_color, fontsize=9.5, fontweight="bold", rotation=0,
                  ha="right", va="center")
    ax.yaxis.set_label_coords(-0.045, 0.5)
    axr.set_ylabel("mm\nrain", color=st.rain_text_color, fontsize=9.5, fontweight="bold", rotation=0,
                   ha="left", va="center")
    axr.yaxis.set_label_coords(1.045, 0.5)
    e = _num(panel.elevation_m)
    elev = "?" if math.isnan(e) else f"{e:.0f}"
    title = f"{panel.label} · {elev} m" if panel.label else f"{elev} m"
    ax.text(0.006, 0.955, title, transform=ax.transAxes, ha="left", va="top", fontsize=10.5,
            fontweight="bold", color="#222222", zorder=8,
            bbox=dict(boxstyle="round,pad=0.2", facecolor=st.background_color, alpha=0.85, edgecolor="none"))


def _footer_lines(data: SnowChartData, tz: ZoneInfo) -> list[str]:
    lines = []
    for s in data.sources:
        elev = "?" if s.grid_elevation_m is None else f"{s.grid_elevation_m:.0f}"
        lines.append(f"{s.role}: {s.model_label} {format_zulu_local(s.run, tz)} · {s.point_label} "
                     f"({s.grid_lat:.3f}, {s.grid_lon:.3f}, model elev {elev} m) · {s.hours}")
    if data.recipe:
        lines.append(data.recipe)
    lines.append(f"Generated {format_zulu_local(data.generated, tz, '%Y-%m-%d %H:%MZ')}")
    return lines


def render_snow_chart(data: SnowChartData, out_path: str | pathlib.Path) -> pathlib.Path:
    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    st = data.style
    tz = ZoneInfo(data.timezone)
    bg = st.background_color

    panels = list(data.panels)
    n = len(panels)
    lines = _footer_lines(data, tz) if st.show_footer else []
    head_in = 1.6
    # tick labels (2 lines) + tz note + footer text
    foot_in = 0.95 + 0.16 * len(lines) if st.show_footer else 0.85
    body_in = st.level_panel_height_in + n * st.panel_height_in
    H = head_in + body_in + foot_in

    plt.rcParams["font.family"] = st.font_family
    plt.rcParams["hatch.linewidth"] = 1.0
    fig = plt.figure(figsize=(st.width_in, H), dpi=st.dpi, facecolor=bg)
    try:
        gs = fig.add_gridspec(1 + n, 1, height_ratios=[st.level_panel_height_in] + [st.panel_height_in] * n,
                              left=0.115, right=0.895, top=1 - head_in / H, bottom=foot_in / H, hspace=0.08)
        x0 = mdates.date2num(data.start)
        x1 = mdates.date2num(data.end)
        if x1 <= x0:
            x1 = x0 + 1

        # --- titles + legend row
        fig.text(0.5, 1 - 0.18 / H, data.title, ha="center", va="top", fontsize=15, fontweight="bold")
        if data.subtitle:
            fig.text(0.5, 1 - 0.50 / H, data.subtitle, ha="center", va="top", fontsize=13, fontweight="bold")
        whisker = Line2D([], [], color=_darker(st.snow_text_color, 0.85), label="Spread (10–90 %)")
        handles = [Patch(facecolor=st.snow_color, alpha=0.8, label=f"{data.bar_hours}hr snow (cm)"),
                   Patch(facecolor=st.rain_color, alpha=0.8, label=f"{data.bar_hours}hr rain (mm)"),
                   Patch(facecolor="#8a9aaa", hatch="///", edgecolor=(1, 1, 1, 0.8), linewidth=0,
                         label="Mixed phase"),
                   whisker,
                   Patch(facecolor="white", edgecolor=st.snow_text_color, label=f"{data.total_hours}hr total")]
        fig.legend(handles=handles, loc="center", bbox_to_anchor=(0.5, 1 - 1.12 / H), ncol=5, fontsize=10,
                   frameon=False, handler_map={whisker: _WhiskerHandler()}, columnspacing=2.2)

        days, within = _time_ticks(data.start, data.end, tz, st.tick_hour_utc, _tick_step(st.min_ticks_per_day))

        ax_level = fig.add_subplot(gs[0])
        _style_time_axis(ax_level, st, days, within, bg)
        _draw_level_panel(ax_level, data, x0, x1)
        _vlines(ax_level, data, x0, x1, label_top=True)
        all_axes = [ax_level]

        for i, panel in enumerate(panels):
            ax = fig.add_subplot(gs[1 + i], sharex=ax_level)
            _style_time_axis(ax, st, days, within, bg)
            axr = ax.twinx()
            # draw rain twin below the snow axes so snow labels / boxes stay on top
            axr.set_zorder(1)
            ax.set_zorder(2)
            ax.patch.set_visible(False)
            axr.patch.set_facecolor(bg)
            axr.patch.set_visible(True)
            axr.grid(False)
            _draw_elevation_panel(ax, axr, panel, data, x0, x1)
            _vlines(ax, data, x0, x1, label_top=False)
            all_axes.append(ax)

        ax_level.set_xlim(x0, x1)
        bottom = all_axes[-1]
        for a in all_axes[:-1]:
            a.tick_params(axis="x", which="both", labelbottom=False)
        bottom.set_xlabel(_tz_note(data.start, data.end, tz, data.timezone), fontsize=9, color=_GREY, labelpad=4)

        # --- footer
        if lines:
            fig.text(0.115, (0.16 * len(lines) + 0.12) / H, "\n".join(lines), ha="left", va="top", fontsize=8,
                     color=_GREY, linespacing=1.4)

        fig.savefig(out_path, dpi=st.dpi, facecolor=bg)
    finally:
        plt.close(fig)
    return out_path
