"""Plain data structures handed from the processing layer to the chart renderer.

The renderer (chart.py) depends only on this module, so charts can be rendered
and tested without any network access or GRIB decoding.
All datetimes are timezone-aware UTC.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class FreezingLevelSegment:
    """A contiguous piece of the freezing-level line coming from one model run."""
    model: str                      # model key, e.g. "hrdps"
    model_label: str                # display name, e.g. "HRDPS"
    run: datetime                   # model initialisation time
    point_label: str
    times: list[datetime]
    freezing_level: list[Optional[float]]           # m ASL, None = undefined
    wetbulb_zero: Optional[list[Optional[float]]] = None  # m ASL (approx. snow level)


@dataclass
class PrecipBar:
    start: datetime
    end: datetime
    mm: float
    model: str
    model_label: str


@dataclass
class PrecipTotal:
    """Longer-period total drawn as a boxed label (e.g. 24 h)."""
    start: datetime
    end: datetime
    mm: float
    complete: bool = True           # False if the period is only partly covered


@dataclass
class WindSample:
    time: datetime
    speed_kmh: float
    direction_deg: float            # meteorological, direction wind blows FROM
    model: str


@dataclass
class ReferenceLine:
    elevation_m: float
    label: str = ""
    color: str = "#4a6b8a"
    style: str = "--"


@dataclass
class SourceInfo:
    """One line of provenance for the footer."""
    role: str                       # "freezing level" / "precipitation" / "wind"
    model_label: str
    run: datetime
    point_label: str
    grid_lat: float
    grid_lon: float
    grid_elevation_m: Optional[float]
    hours: str                      # e.g. "0-48h"


@dataclass
class ChartStyle:
    width_in: float = 11.0
    height_in: float = 11.0
    dpi: int = 110
    elevation_min_m: float = 0
    elevation_max_m: float = 4000
    precip_axis_max_mm: Optional[float] = None   # None = auto
    precip_axis_fraction: float = 0.42           # fraction of the plot height the precip axis spans
    show_bar_labels: bool = True
    show_wetbulb_zero: bool = True
    show_model_transitions: bool = True
    show_footer: bool = True
    show_now_line: bool = True
    tick_hour_utc: Optional[int] = None           # day ticks at this UTC hour (None = local midnight)
    min_ticks_per_day: int = 1                    # extra ticks within the day (step divides 24 h)
    freezing_level_color: str = "#a3232b"
    wetbulb_color: str = "#3a6fb0"
    precip_color: str = "#5aa65a"
    precip_text_color: str = "#1f6b1f"
    background_color: str = "#e8e8e8"
    font_family: str = "DejaVu Sans"


@dataclass
class ChartData:
    chart_id: str
    title: str
    subtitle: str
    timezone: str                   # IANA tz for axis labels, e.g. "America/Vancouver"
    start: datetime
    end: datetime
    generated: datetime
    freezing_level: list[FreezingLevelSegment] = field(default_factory=list)
    precip_bars: list[PrecipBar] = field(default_factory=list)
    precip_totals: list[PrecipTotal] = field(default_factory=list)
    precip_bar_hours: int = 6
    precip_total_hours: int = 24
    wind: list[WindSample] = field(default_factory=list)
    wind_elevation_m: Optional[float] = None
    reference_lines: list[ReferenceLine] = field(default_factory=list)
    sources: list[SourceInfo] = field(default_factory=list)
    style: ChartStyle = field(default_factory=ChartStyle)
