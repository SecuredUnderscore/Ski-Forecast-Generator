"""Render a meteogram PNG (freezing level + precipitation) from ChartData."""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from .chartdata import ChartData  # noqa: E402


def _nan(values):
    return np.array([np.nan if v is None else float(v) for v in values], dtype=float)


def _fmt_mm(mm: float) -> str:
    if 0 < mm < 1:
        return f"{mm:.1f}"
    return f"{mm:.0f}"


def _day_ticks(start: datetime, end: datetime, tz: ZoneInfo, hour: int) -> list[datetime]:
    local = start.astimezone(tz)
    day = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if day < local:
        day += timedelta(days=1)
    ticks = []
    while day <= end.astimezone(tz):
        ticks.append(day)
        # step by calendar day in local time (handles DST)
        nxt = (day + timedelta(days=1, hours=2)).replace(hour=hour)
        day = nxt
    return ticks


def _tick_label(t: datetime) -> str:
    h = t.hour % 12 or 12
    ampm = "am" if t.hour < 12 else "pm"
    return f"{t.strftime('%a')}{t.day:02d}\n{h}{ampm}"


def render_chart(data: ChartData, out_path: str | pathlib.Path) -> pathlib.Path:
    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    st = data.style
    tz = ZoneInfo(data.timezone)

    plt.rcParams["font.family"] = st.font_family
    fig = plt.figure(figsize=(st.width_in, st.height_in), dpi=st.dpi, facecolor=st.background_color)
    try:
        bottom = 0.16 if st.show_footer else 0.09
        ax = fig.add_axes([0.14, bottom, 0.73, 0.90 - bottom])
        ax.set_facecolor(st.background_color)
        ax2 = ax.twinx()

        # --- titles
        fig.text(0.5, 0.975, data.title, ha="center", va="top", fontsize=15, fontweight="bold")
        if data.subtitle:
            fig.text(0.5, 0.945, data.subtitle, ha="center", va="top", fontsize=13, fontweight="bold")

        x0 = mdates.date2num(data.start)
        x1 = mdates.date2num(data.end)
        if x1 <= x0:
            x1 = x0 + 1
        ax.set_xlim(x0, x1)
        ax.set_ylim(st.elevation_min_m, st.elevation_max_m)

        # --- precipitation bars (right axis, lower part of the plot)
        ax2.set_zorder(1)
        ax.set_zorder(2)
        ax.patch.set_visible(False)
        fig.patch.set_facecolor(st.background_color)
        bars = data.precip_bars
        pmax = st.precip_axis_max_mm
        if pmax is None:
            pmax = max([10.0] + [b.mm * 1.15 for b in bars])
        frac = min(max(st.precip_axis_fraction, 0.05), 1.0)
        ax2.set_ylim(0, pmax / frac)
        for b in bars:
            bx = mdates.date2num(b.start)
            bw = mdates.date2num(b.end) - bx
            ax2.bar(bx, b.mm, width=bw * 0.92, align="edge", color=st.precip_color,
                    alpha=0.75, edgecolor="none", zorder=1)
            if st.show_bar_labels and b.mm >= 0.05:
                ax2.text(bx + bw / 2, b.mm + pmax * 0.012, _fmt_mm(b.mm), ha="center", va="bottom",
                         fontsize=9 if b.mm >= 1 else 7.5, fontweight="bold", color=st.precip_text_color, zorder=3)
        step = max(1, int(round(pmax / 5)))
        ticks = list(range(step, int(pmax) + 1, step))
        ax2.set_yticks(ticks)
        ax2.tick_params(axis="y", colors=st.precip_text_color, labelsize=11)
        ax2.spines["right"].set_color(st.precip_text_color)
        ax2.spines["right"].set_bounds(0, pmax)
        ax2.set_ylabel(f"{data.precip_bar_hours}hr\npcpn\nmm", color=st.precip_text_color, fontsize=12,
                       fontweight="bold", rotation=0, ha="left", va="center")
        ax2.yaxis.set_label_coords(1.05, frac * 0.75)

        # --- boxed totals
        box_y = 0.40
        for tot in data.precip_totals:
            cx = (mdates.date2num(tot.start) + mdates.date2num(tot.end)) / 2
            if not (x0 <= cx <= x1):
                continue
            ax.text(cx, box_y, _fmt_mm(tot.mm), transform=ax.get_xaxis_transform(), ha="center",
                    va="center", fontsize=12, fontweight="bold", color=st.precip_text_color, zorder=6,
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.85,
                              edgecolor=st.precip_text_color,
                              linestyle="-" if tot.complete else "--", linewidth=1.3))

        # --- reference lines
        for rl in data.reference_lines:
            if not (st.elevation_min_m <= rl.elevation_m <= st.elevation_max_m):
                continue
            ax.axhline(rl.elevation_m, color=rl.color, linestyle=rl.style, linewidth=1.2, zorder=2)
            if rl.label:
                ax.text(0.005, rl.elevation_m, f" {rl.label} {rl.elevation_m:.0f} m",
                        transform=ax.get_yaxis_transform(), ha="left", va="bottom",
                        fontsize=8.5, color=rl.color, zorder=6)

        # --- freezing level / wet-bulb lines
        legend_items = []
        have_fl = have_wb = False
        prev_last = None
        for seg in data.freezing_level:
            if not seg.times:
                continue
            xs = mdates.date2num(seg.times)
            fl = _nan(seg.freezing_level)
            # join visually to previous segment
            if prev_last is not None and not np.isnan(prev_last[1]) and len(fl) and not np.isnan(fl[0]):
                ax.plot([prev_last[0], xs[0]], [prev_last[1], fl[0]], color=st.freezing_level_color,
                        linewidth=2.2, zorder=5)
            ax.plot(xs, fl, color=st.freezing_level_color, linewidth=2.2, zorder=5)
            have_fl = True
            prev_last = (xs[-1], fl[-1])
            if st.show_wetbulb_zero and seg.wetbulb_zero is not None:
                ax.plot(xs, _nan(seg.wetbulb_zero), color=st.wetbulb_color, linewidth=1.4,
                        linestyle="--", zorder=4)
                have_wb = True

        # --- model transitions
        if st.show_model_transitions:
            segs = [s for s in data.freezing_level if s.times]
            for a, b in zip(segs, segs[1:]):
                if a.model == b.model and a.run == b.run:
                    continue
                tx = mdates.date2num(b.times[0])
                if x0 < tx < x1:
                    ax.axvline(tx, color="#777777", linestyle=":", linewidth=1.2, zorder=3)
                    ax.text(tx, 1.004, f"{a.model_label}→{b.model_label}", transform=ax.get_xaxis_transform(),
                            ha="center", va="bottom", fontsize=8.5, color="#555555", zorder=6)

        # --- now line
        if st.show_now_line:
            nx = mdates.date2num(data.generated)
            if x0 < nx < x1:
                ax.axvline(nx, color="#333333", linewidth=1.0, alpha=0.6, zorder=3)

        # --- wind barbs along the top
        if data.wind:
            wx = mdates.date2num([w.time for w in data.wind])
            spd_kt = np.array([w.speed_kmh for w in data.wind]) / 1.852
            rad = np.deg2rad([w.direction_deg for w in data.wind])
            u = -spd_kt * np.sin(rad)
            v = -spd_kt * np.cos(rad)
            inr = (wx >= x0) & (wx <= x1)
            yb = st.elevation_min_m + 0.90 * (st.elevation_max_m - st.elevation_min_m)
            ax.barbs(wx[inr], np.full(inr.sum(), yb), u[inr], v[inr], length=5.5, linewidth=0.8,
                     color="#333333", zorder=6)
            lbl = "Wind" if data.wind_elevation_m is None else f"Wind @ {data.wind_elevation_m:.0f} m"
            ax.text(0.995, 0.955, lbl + " (barbs, kt)", transform=ax.transAxes, ha="right", va="bottom",
                    fontsize=8.5, color="#333333", zorder=6)

        # --- axes cosmetics
        ax.set_ylabel("Elevation\nasl (m)", fontsize=13, fontweight="bold", rotation=0, ha="right", va="center",
                      labelpad=10)
        ax.yaxis.set_label_coords(-0.075, 0.6)
        ax.tick_params(axis="both", labelsize=11)
        ax.grid(True, axis="x", color="white", linewidth=1.0, alpha=0.9)
        ax.grid(True, axis="y", color="white", linewidth=0.6, alpha=0.6)
        ticks = _day_ticks(data.start, data.end, tz, st.tick_hour_local)
        ax.set_xticks(mdates.date2num(ticks))
        ax.set_xticklabels([_tick_label(t) for t in ticks])
        for side in ("top", "left", "bottom"):
            ax.spines[side].set_color("#555555")
        ax2.spines["top"].set_visible(False)

        # --- legend
        legend_items.append(Line2D([], [], color=st.freezing_level_color, linewidth=2.2, label="Freezing level"))
        if have_wb:
            legend_items.append(Line2D([], [], color=st.wetbulb_color, linewidth=1.4, linestyle="--",
                                       label="Wet-bulb 0°C (≈ snow level)"))
        legend_items.append(Patch(facecolor=st.precip_color, alpha=0.75, label=f"{data.precip_bar_hours}hr Total"))
        legend_items.append(Patch(facecolor="white", edgecolor=st.precip_text_color, label=f"{data.precip_total_hours}hr Total"))
        if not have_fl:
            legend_items[0].set_label("Freezing level (no data)")
        ax.legend(handles=legend_items, loc="upper left", fontsize=10.5, framealpha=0.9,
                  bbox_to_anchor=(0.005, 0.85 if data.wind else 0.995))

        # --- footer
        if st.show_footer:
            lines = []
            for s in data.sources:
                elev = "?" if s.grid_elevation_m is None else f"{s.grid_elevation_m:.0f}"
                lines.append(f"{s.role}: {s.model_label} {s.run:%Y-%m-%d %HZ} · {s.point_label} "
                             f"({s.grid_lat:.3f}, {s.grid_lon:.3f}, model elev {elev} m) · {s.hours}")
            gen = data.generated.astimezone(tz)
            lines.append(f"Generated {gen:%Y-%m-%d %H:%M %Z}")
            fig.text(0.14, bottom - 0.075, "\n".join(lines), ha="left", va="top", fontsize=8,
                     color="#555555", linespacing=1.4)

        fig.savefig(out_path, dpi=st.dpi, facecolor=st.background_color)
    finally:
        plt.close(fig)
    return out_path
