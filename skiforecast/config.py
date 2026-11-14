"""Configuration loading and validation.

The whole service is driven by one YAML file.  ``${ENV_VAR}`` and
``${ENV_VAR:-default}`` references are expanded before parsing so secrets
(webhook URLs) can live in a .env file.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Annotated, Literal, Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import BUILTIN_MODELS

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_WEBHOOK_RE = re.compile(r"^https://(?:\w+\.)?(?:discord|discordapp)\.com/api/webhooks/\d+/[\w-]+$")


class ConfigError(Exception):
    pass


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- models
class ModelConfig(Strict):
    label: str
    description: str = ""
    directory_template: str
    profile_file_template: str
    surface_file_template: str
    variables: dict[str, str]
    runs: list[int]
    max_hour: int = Field(gt=0)
    profile_step_hours: int = Field(ge=1)
    profile_schedule: Optional[list[tuple[int, int]]] = None   # [[until_hour, step], ...]
    precip_step_hours: int = Field(ge=1)
    levels: list[int]
    typical_delay_hours: float = 3.0
    lookback_hours: int = 30
    stale_after_hours: float = 18
    enabled: bool = True

    @field_validator("runs")
    @classmethod
    def _runs(cls, v):
        if not v or any(r < 0 or r > 23 for r in v):
            raise ValueError("runs must be a non-empty list of UTC hours 0-23")
        return sorted(set(v))

    @model_validator(mode="after")
    def _vars(self):
        required = {"temperature", "height", "surface_pressure", "precip_accum"}
        missing = required - set(self.variables)
        if missing:
            raise ValueError(f"variables missing required roles: {sorted(missing)}")
        return self


# --------------------------------------------------------------------------- points
class PointConfig(Strict):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=360)
    label: Optional[str] = None
    elevation_m: Optional[float] = None   # real-world elevation, shown in footer only


# --------------------------------------------------------------------------- charts
class SourceConfig(Strict):
    """One model segment of a chart layer.

    Segments are used in order: each covers valid times after the previous
    segment ends, up to its own ``until_hour`` (hours after *its own* run
    start) or the end of its data.
    """
    model: str
    point: str
    offset: tuple[int, int] = (0, 0)      # grid cells (east, north), e.g. [-1, -1] = 1 left, 1 down
    until_hour: Optional[int] = None


class ReferenceLineConfig(Strict):
    elevation: float
    label: str = ""
    color: str = "#4a6b8a"
    style: str = "--"


class WindConfig(Strict):
    enabled: bool = False
    elevation_m: float = 2200
    every_hours: int = 6
    source: Literal["freezing_level", "precipitation"] = "freezing_level"


class StyleConfig(Strict):
    width_in: float = 11.0
    height_in: float = 11.0
    dpi: int = 110
    elevation_min_m: float = 0
    elevation_max_m: float = 4000
    precip_axis_max_mm: Optional[float] = None
    precip_axis_fraction: float = Field(0.42, gt=0.05, le=1.0)
    show_bar_labels: bool = True
    show_model_transitions: bool = True
    show_footer: bool = True
    show_now_line: bool = True
    tick_hour_utc: Optional[int] = Field(None, ge=0, le=23)  # UTC hour of the daily x-axis marker (None = local midnight)
    min_ticks_per_day: int = Field(1, ge=1, le=11)        # 1 = daily ... 7-11 = every 2-3 h
    freezing_level_color: str = "#a3232b"
    wetbulb_color: str = "#3a6fb0"
    precip_color: str = "#5aa65a"
    precip_text_color: str = "#1f6b1f"
    background_color: str = "#e8e8e8"


_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class PostConfig(Strict):
    """When a chart is (re-)posted to Discord.

    Default: whenever any model the chart uses has a new run.
    ``models``: only new runs of these models trigger a post (other models'
                runs are still used for the data when the post happens).
    ``times``:  local wall-clock times (HH:MM).  When set, the chart is posted
                on this schedule with the latest data instead of on model updates.
    """
    models: list[str] = []
    times: list[str] = []

    @field_validator("times")
    @classmethod
    def _times(cls, v):
        out = []
        for t in v:
            m = _TIME_RE.match(str(t).strip())
            if not m:
                raise ValueError(f"post time {t!r} must be HH:MM (24 h)")
            out.append(f"{int(m.group(1)):02d}:{m.group(2)}")
        return sorted(set(out))

    @property
    def scheduled(self) -> bool:
        return bool(self.times)


class ChartBase(Strict):
    """Fields shared by every chart type."""
    id: str
    title: str
    enabled: bool = True
    webhook: Optional[str] = None              # falls back to discord.default_webhook
    message: str = ""                          # optional text posted with the image
    history_hours: int = Field(0, ge=0)        # hours before "now" to keep on the chart
    trim_to_data: bool = False                 # end the chart where the model data ends
    min_post_interval_minutes: int = Field(0, ge=0)
    post: PostConfig = PostConfig()

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
            raise ValueError("chart id may only contain letters, digits, '_' and '-'")
        return v

    def all_sources(self) -> list[SourceConfig]:          # overridden
        return []

    def models(self) -> list[str]:
        """Model keys used by this chart, in first-use order."""
        return list(dict.fromkeys(s.model for s in self.all_sources()))


class ChartConfig(ChartBase):
    """Classic meteogram: freezing level line(s) + precipitation bars."""
    type: Literal["meteogram"] = "meteogram"
    subtitle: str = "Freezing Level · Precipitation"
    hours: int = Field(240, gt=0)              # maximum chart length from start
    freezing_level: list[SourceConfig]
    precipitation: Optional[list[SourceConfig]] = None   # None = same as freezing_level
    freezing_level_method: Literal["highest", "lowest"] = "highest"
    below_ground: Literal["extrapolate", "clamp", "hide"] = "extrapolate"
    show_wetbulb_zero: bool = True
    precip_bar_hours: int = Field(6, ge=1, le=24)
    precip_total_hours: int = Field(24, ge=1, le=72)
    precip_total_start_hour_local: Optional[int] = Field(None, ge=0, le=23)  # None = style.tick_hour_utc, else local midnight
    wind: WindConfig = WindConfig()
    reference_lines: list[ReferenceLineConfig] = []
    style: StyleConfig = StyleConfig()

    @field_validator("freezing_level")
    @classmethod
    def _nonempty(cls, v):
        if not v:
            raise ValueError("freezing_level needs at least one source")
        return v

    @property
    def precip_sources(self) -> list[SourceConfig]:
        return self.precipitation if self.precipitation is not None else self.freezing_level

    def all_sources(self) -> list[SourceConfig]:
        return self.freezing_level + self.precip_sources


# --------------------------------------------------------------------------- blended snow chart
class ElevationConfig(Strict):
    elevation: float = Field(ge=-500, le=9000)   # m ASL
    label: str = ""


class BlendConfig(Strict):
    """How the spread (margin of error) is built.  All of it comes from data the
    service already downloads: recent runs of the same model, the grid cells
    around the point, and fixed perturbations of the phase/ratio physics."""
    lagged_runs: int = Field(3, ge=1, le=10)            # recent runs of each model to blend (<= general.keep_runs)
    neighbourhood_km: float = Field(6.0, ge=0, le=50)   # radius around the point; 0 = centre cell only
    temperature_sigma_c: float = Field(1.0, ge=0, le=5) # wet-bulb perturbation (+/-) for the phase decision
    ratio_spread: float = Field(0.3, ge=0, le=1)        # +/- fraction on the snow-to-liquid ratio
    widen: float = Field(1.5, ge=1, le=5)               # multiply the raw spread (ensembles are under-dispersive)
    use_wind: bool = False                              # lower the ratio in strong wind (downloads wind files)
    snow_level_offset_m: float = Field(150, ge=0, le=1000)  # snow reaches this far below the wet-bulb 0 C height


class SnowStyleConfig(Strict):
    width_in: float = 11.0
    panel_height_in: float = 2.4            # per elevation panel
    level_panel_height_in: float = 2.6      # the snow-level panel on top
    dpi: int = 110
    show_bar_labels: bool = True            # centre value above each bar
    show_bar_ranges: bool = False           # also print "low-high" under each bar label
    show_model_transitions: bool = True
    show_footer: bool = True
    show_now_line: bool = True
    show_freezing_level: bool = True        # thin freezing-level line on the snow-level panel
    tick_hour_utc: Optional[int] = Field(None, ge=0, le=23)
    min_ticks_per_day: int = Field(1, ge=1, le=11)
    snow_axis_max_cm: Optional[float] = None     # None = auto per panel
    rain_axis_max_mm: Optional[float] = None
    snow_color: str = "#3a6fb0"
    snow_text_color: str = "#24487a"
    rain_color: str = "#5aa65a"
    rain_text_color: str = "#1f6b1f"
    snow_level_color: str = "#3a6fb0"
    freezing_level_color: str = "#a3232b"
    background_color: str = "#e8e8e8"
    show_slot_separators: bool = True       # thin line between slots so each snow/rain pair reads as one
    slot_separator_color: str = "#c9c9c9"


class SnowChartConfig(ChartBase):
    """Blended Snow Forecast: stacked snow/rain panels, one per elevation, with
    a spread built from recent runs, neighbouring cells and physics perturbations."""
    type: Literal["blended_snow"]
    subtitle: str = "Blended Snow Forecast"
    hours: int = Field(48, gt=0)
    sources: list[SourceConfig]                 # one chain for profile + precipitation
    elevations: list[ElevationConfig]
    bar_hours: int = Field(3, ge=1, le=24)
    total_hours: int = Field(24, ge=1, le=72)
    total_start_hour_local: Optional[int] = Field(None, ge=0, le=23)   # None = style.tick_hour_utc, else local midnight
    start_at_run: bool = True                   # chart runs from the first source's run start (a 12Z run -> 12Z to
                                                # 12Z + hours); False = from now - history_hours like the meteogram
    blend: BlendConfig = BlendConfig()
    style: SnowStyleConfig = SnowStyleConfig()

    @field_validator("elevations", mode="before")
    @classmethod
    def _elevs(cls, v):
        if not isinstance(v, list):
            raise ValueError("elevations must be a list")
        return [{"elevation": e} if isinstance(e, (int, float)) else e for e in v]

    @field_validator("elevations")
    @classmethod
    def _elevs_count(cls, v):
        if not 1 <= len(v) <= 5:
            raise ValueError("elevations needs 1 to 5 entries")
        return sorted(v, key=lambda e: -e.elevation)      # highest first (top panel)

    @field_validator("sources")
    @classmethod
    def _sources(cls, v):
        if not v:
            raise ValueError("sources needs at least one entry")
        return v

    def all_sources(self) -> list[SourceConfig]:
        return list(self.sources)


AnyChart = Annotated[Union[ChartConfig, SnowChartConfig], Field(discriminator="type")]


# --------------------------------------------------------------------------- general
class GeneralConfig(Strict):
    timezone: str = "America/Vancouver"
    poll_interval_minutes: float = Field(5, ge=1)
    data_dir: str = "/data"
    download_workers: int = Field(6, ge=1, le=32)
    http_timeout_seconds: float = 60
    http_retries: int = 3
    profile_top_hpa: int = Field(600, ge=50, le=1000)   # highest level (lowest pressure) to download
    max_missing_fraction: float = Field(0.03, ge=0, le=0.5)  # tolerate a few unpublished files per run
    keep_runs: int = Field(3, ge=1)                     # processed runs kept per model
    keep_charts: int = Field(20, ge=1)                  # rendered PNGs kept per chart
    post_on_startup: bool = True
    user_agent: str = "ski-forecast-generator/1.0"

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v


class DiscordConfig(Strict):
    enabled: bool = True
    default_webhook: Optional[str] = None
    alerts_webhook: Optional[str] = None          # errors / stale models / startup
    username: str = "Ski Forecast"
    avatar_url: Optional[str] = None
    startup_message: bool = True
    alert_repeat_hours: float = 6                 # don't repeat the same alert more often


class AppConfig(Strict):
    general: GeneralConfig = GeneralConfig()
    discord: DiscordConfig = DiscordConfig()
    points: dict[str, PointConfig]
    models: dict[str, dict] = {}                   # overrides / additional models (raw, merged below)
    charts: list[AnyChart]

    # filled by load_config
    resolved_models: dict[str, ModelConfig] = {}

    @model_validator(mode="before")
    @classmethod
    def _default_chart_type(cls, data):
        # charts without `type:` are classic meteograms (backwards compatible)
        if isinstance(data, dict) and isinstance(data.get("charts"), list):
            data = dict(data)
            data["charts"] = [{"type": "meteogram", **c} if isinstance(c, dict) and "type" not in c else c
                              for c in data["charts"]]
        return data

    # ------------------------------------------------------------------ helpers
    def enabled_charts(self) -> list[ChartBase]:
        return [c for c in self.charts if c.enabled]

    def used_models(self) -> set[str]:
        out = set()
        for c in self.enabled_charts():
            out.update(c.models())
        return out

    def webhook_for(self, chart: ChartBase) -> Optional[str]:
        return chart.webhook or self.discord.default_webhook


# --------------------------------------------------------------------------- loading
def _expand_env(text: str) -> str:
    def repl(m):
        val = os.environ.get(m.group(1))
        if val is None or val == "":
            val = m.group(2) if m.group(2) is not None else ""
        return val
    return _ENV_RE.sub(repl, text)


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    raw_text = _expand_env(path.read_text(encoding="utf-8"))
    try:
        raw = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML syntax error: {e}") from e
    try:
        cfg = AppConfig.model_validate(raw)
    except Exception as e:  # pydantic ValidationError -> readable message
        raise ConfigError(str(e)) from e

    # Resolve models: built-ins merged with overrides, plus fully custom models.
    resolved: dict[str, ModelConfig] = {}
    errors: list[str] = []
    for key in set(BUILTIN_MODELS) | set(cfg.models):
        merged = _merge(BUILTIN_MODELS.get(key, {}), cfg.models.get(key) or {})
        try:
            resolved[key] = ModelConfig.model_validate(merged)
        except Exception as e:
            errors.append(f"models.{key}: {e}")
    cfg.resolved_models = resolved

    errors += _cross_validate(cfg)
    if errors:
        raise ConfigError("Configuration problems:\n  - " + "\n  - ".join(errors))
    return cfg


def _cross_validate(cfg: AppConfig) -> list[str]:
    errs: list[str] = []
    ids = [c.id for c in cfg.charts]
    for dup in {i for i in ids if ids.count(i) > 1}:
        errs.append(f"duplicate chart id {dup!r}")
    if not cfg.enabled_charts():
        errs.append("no enabled charts")

    for hook_name, hook in (("discord.default_webhook", cfg.discord.default_webhook),
                            ("discord.alerts_webhook", cfg.discord.alerts_webhook)):
        if hook and not _WEBHOOK_RE.match(hook):
            errs.append(f"{hook_name} does not look like a Discord webhook URL")

    for c in cfg.charts:
        where = f"charts[{c.id}]"
        hook = cfg.webhook_for(c)
        if cfg.discord.enabled and c.enabled and not hook:
            errs.append(f"{where}: no webhook (set charts[].webhook or discord.default_webhook)")
        if c.webhook and not _WEBHOOK_RE.match(c.webhook):
            errs.append(f"{where}.webhook does not look like a Discord webhook URL")
        for mk in c.post.models:
            if mk not in c.models():
                errs.append(f"{where}.post.models: {mk!r} is not used by this chart (uses {c.models()})")
        if isinstance(c, ChartConfig):
            layers = (("freezing_level", c.freezing_level), ("precipitation", c.precip_sources))
        else:
            layers = (("sources", c.sources),)
        for layer, sources in layers:
            for i, s in enumerate(sources):
                w = f"{where}.{layer}[{i}]"
                m = cfg.resolved_models.get(s.model)
                if m is None:
                    errs.append(f"{w}: unknown model {s.model!r} (known: {sorted(cfg.resolved_models)})")
                elif not m.enabled:
                    errs.append(f"{w}: model {s.model!r} is disabled")
                elif s.until_hour is not None and s.until_hour > m.max_hour:
                    errs.append(f"{w}: until_hour {s.until_hour} exceeds {s.model} max_hour {m.max_hour}")
                if s.point not in cfg.points:
                    errs.append(f"{w}: unknown point {s.point!r} (known: {sorted(cfg.points)})")
                if isinstance(c, ChartConfig):
                    if m and layer == "freezing_level" and c.show_wetbulb_zero and "dewpoint_depression" not in m.variables:
                        errs.append(f"{w}: model {s.model} has no dewpoint_depression variable; set show_wetbulb_zero: false")
                    if m and c.wind.enabled and not {"wind_speed", "wind_direction"} <= set(m.variables):
                        errs.append(f"{w}: model {s.model} has no wind variables; disable wind")
                else:
                    if m and "dewpoint_depression" not in m.variables:
                        errs.append(f"{w}: model {s.model} has no dewpoint_depression variable (needed for the wet-bulb snow phase)")
                    if m and c.blend.use_wind and not {"wind_speed", "wind_direction"} <= set(m.variables):
                        errs.append(f"{w}: model {s.model} has no wind variables; set blend.use_wind: false")
        if isinstance(c, ChartConfig):
            if c.precip_total_hours % c.precip_bar_hours:
                errs.append(f"{where}: precip_total_hours must be a multiple of precip_bar_hours")
            s = c.style
            if s.elevation_max_m <= s.elevation_min_m:
                errs.append(f"{where}.style: elevation_max_m must exceed elevation_min_m")
        else:
            if c.total_hours % c.bar_hours:
                errs.append(f"{where}: total_hours must be a multiple of bar_hours")
            if c.blend.lagged_runs > cfg.general.keep_runs:
                errs.append(f"{where}: blend.lagged_runs ({c.blend.lagged_runs}) exceeds general.keep_runs "
                            f"({cfg.general.keep_runs}); raise keep_runs")
    return errs
