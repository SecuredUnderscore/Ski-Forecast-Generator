"""MSC Datamart access: run discovery, completeness checks and point extraction.

A run is processed by downloading only the files we need, decoding the values
at the configured grid points and immediately deleting each file, so disk use
stays tiny even though the source grids are national/global.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import requests

from .config import AppConfig, ModelConfig
from . import grib

log = logging.getLogger(__name__)
UTC = timezone.utc
_HREF_RE = re.compile(r'href="([^"?/][^"]*)"')


# --------------------------------------------------------------------------- helpers
def fmt(template: str, init: datetime, hour: int, var: str = "", level: int = 0) -> str:
    return template.format(date=init.strftime("%Y%m%d"), run=f"{init.hour:02d}",
                           hour=f"{hour:03d}", var=var, level=f"{level:04d}")


def run_id(init: datetime) -> str:
    return init.strftime("%Y%m%dT%HZ")


def parse_run_id(s: str) -> datetime:
    return datetime.strptime(s, "%Y%m%dT%HZ").replace(tzinfo=UTC)


class Http:
    def __init__(self, user_agent: str, timeout: float, retries: int):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = user_agent
        self.timeout = timeout
        self.retries = retries

    def get(self, url: str, stream=False) -> requests.Response:
        last = None
        for attempt in range(self.retries + 1):
            try:
                r = self.s.get(url, timeout=self.timeout, stream=stream)
                if r.status_code == 404:
                    return r
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                last = e
                time.sleep(min(2 ** attempt, 20))
        raise last  # type: ignore[misc]

    def listing(self, url: str) -> Optional[set[str]]:
        r = self.get(url)
        if r.status_code == 404:
            return None
        return set(_HREF_RE.findall(r.text))


# --------------------------------------------------------------------------- requirements
@dataclass(frozen=True)
class PointReq:
    key: str            # "<point>@<east>,<north>"
    point: str
    lat: float
    lon: float
    offset: tuple[int, int]


@dataclass
class ModelNeeds:
    """What the enabled charts need from one model."""
    model: str
    points: dict[str, PointReq] = field(default_factory=dict)
    profile_roles: set[str] = field(default_factory=set)   # e.g. temperature, height...
    need_profile: bool = False
    need_precip: bool = False
    max_hour: int = 0

    def signature(self) -> str:
        return json.dumps({"points": sorted(self.points), "roles": sorted(self.profile_roles),
                           "profile": self.need_profile, "precip": self.need_precip,
                           "max_hour": self.max_hour}, sort_keys=True)


def point_key(point: str, offset) -> str:
    return f"{point}@{offset[0]},{offset[1]}"


def compute_needs(cfg: AppConfig) -> dict[str, ModelNeeds]:
    needs: dict[str, ModelNeeds] = {}
    for c in cfg.enabled_charts():
        layers = [("fl", c.freezing_level), ("precip", c.precip_sources)]
        for layer, sources in layers:
            for s in sources:
                m = cfg.resolved_models[s.model]
                n = needs.setdefault(s.model, ModelNeeds(s.model))
                p = cfg.points[s.point]
                k = point_key(s.point, s.offset)
                n.points[k] = PointReq(k, s.point, p.lat, p.lon, tuple(s.offset))
                n.max_hour = max(n.max_hour, min(s.until_hour or m.max_hour, m.max_hour))
                wind_here = c.wind.enabled and (
                    (c.wind.source == "freezing_level" and layer == "fl") or
                    (c.wind.source == "precipitation" and layer == "precip"))
                if layer == "fl" or wind_here:
                    n.need_profile = True
                    n.profile_roles |= {"temperature", "height"}
                    if layer == "fl" and c.show_wetbulb_zero:
                        n.profile_roles.add("dewpoint_depression")
                    if wind_here:
                        n.profile_roles |= {"wind_speed", "wind_direction"}
                if layer == "precip":
                    n.need_precip = True
    return needs


def profile_hours(m: ModelConfig, max_hour: int) -> list[int]:
    if not m.profile_schedule:
        return list(range(0, max_hour + 1, m.profile_step_hours))
    hours, h = [0], 0
    for until, step in sorted(m.profile_schedule):
        # a user-set profile_step_hours larger than the schedule step wins
        step = max(step, m.profile_step_hours)
        while h + step <= min(until, max_hour):
            h += step
            hours.append(h)
    return hours


def precip_hours(m: ModelConfig, max_hour: int) -> list[int]:
    return list(range(m.precip_step_hours, max_hour + 1, m.precip_step_hours))


# --------------------------------------------------------------------------- worker
def _extract_job(url: str, indices: list[int], tmp_dir: str, ua: str, timeout: float, retries: int):
    """Runs in a worker process: download one file, return values at indices."""
    http = Http(ua, timeout, retries)
    r = http.get(url, stream=True)
    if r.status_code == 404:
        return url, None, "", 0
    fd, path = tempfile.mkstemp(suffix=".grib2", dir=tmp_dir)
    size = 0
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                size += len(chunk)
        vals, units = grib.extract_values(path, indices)
        return url, vals, units, size
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- client
class RunIncomplete(Exception):
    pass


class Datamart:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        g = cfg.general
        self.http = Http(g.user_agent, g.http_timeout_seconds, g.http_retries)
        self.data_dir = Path(g.data_dir)
        self.tmp_dir = self.data_dir / "tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.grid_dir = self.data_dir / "grids"
        self.grid_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- discovery
    def candidate_runs(self, m: ModelConfig, now: datetime) -> list[datetime]:
        """Run start times inside the lookback window, newest first."""
        out = []
        t = now.replace(minute=0, second=0, microsecond=0)
        earliest = now - timedelta(hours=m.lookback_hours)
        while t >= earliest:
            if t.hour in m.runs and t <= now - timedelta(hours=m.typical_delay_hours * 0.6):
                out.append(t)
            t -= timedelta(hours=1)
        return out

    def required_files(self, m: ModelConfig, needs: ModelNeeds, init: datetime, levels: list[int]):
        """Yield (hour, role, level|None, filename)."""
        if needs.need_profile:
            for h in profile_hours(m, needs.max_hour):
                yield h, "surface_pressure", None, fmt(m.surface_file_template, init, h, m.variables["surface_pressure"])
                for role in sorted(needs.profile_roles):
                    for lv in levels:
                        yield h, role, lv, fmt(m.profile_file_template, init, h, m.variables[role], lv)
        if needs.need_precip:
            for h in precip_hours(m, needs.max_hour):
                yield h, "precip_accum", None, fmt(m.surface_file_template, init, h, m.variables["precip_accum"])

    def available_levels(self, m: ModelConfig, init: datetime, hour: int, listing: set[str]) -> list[int]:
        top = self.cfg.general.profile_top_hpa
        return [lv for lv in m.levels if lv >= top and
                fmt(m.profile_file_template, init, hour, m.variables["temperature"], lv) in listing]

    def check_complete(self, m: ModelConfig, needs: ModelNeeds, init: datetime) -> Optional[list[int]]:
        """Return the usable pressure levels if every needed file of the run's
        first and last needed hours is published, else None."""
        last = needs.max_hour
        hours_to_check = sorted({0, last} | ({profile_hours(m, last)[-1]} if needs.need_profile else set())
                                | ({precip_hours(m, last)[-1]} if needs.need_precip and precip_hours(m, last) else set()))
        listings = {}
        for h in hours_to_check:
            lst = self.http.listing(fmt(m.directory_template, init, h))
            if lst is None:
                return None
            listings[h] = lst
        levels = [0]
        if needs.need_profile:
            levels = self.available_levels(m, init, 0, listings[0])
            if len(levels) < 3:
                return None
        for h, role, lv, name in self.required_files(m, needs, init, levels):
            if h in listings and name not in listings[h]:
                return None
        return levels

    # ------------------------------------------------------------- grid points
    def resolve_points(self, key: str, m: ModelConfig, needs: ModelNeeds, init: datetime) -> dict:
        """Locate grid cells for every needed point (cached per grid signature)."""
        url = fmt(m.directory_template, init, 0) + fmt(m.surface_file_template, init, 0, m.variables["surface_pressure"])
        r = self.http.get(url, stream=True)
        if r.status_code == 404:
            raise RunIncomplete(f"{url} missing")
        fd, path = tempfile.mkstemp(suffix=".grib2", dir=self.tmp_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
            g = grib.read_grid(path)
        finally:
            os.remove(path)
        out = {}
        for pk, p in needs.points.items():
            j, i = grib.nearest_index(g, p.lat, p.lon, p.offset)
            out[pk] = {"point": p.point, "lat": p.lat, "lon": p.lon, "offset": list(p.offset),
                       "j": j, "i": i, "index": j * g.ni + i,
                       "grid_lat": round(float(g.lats[j, i]), 5), "grid_lon": round(float(g.lons[j, i]), 5)}
        (self.grid_dir / f"{key}.json").write_text(json.dumps({"signature": g.signature, "points": out}, indent=1))
        return out

    # ------------------------------------------------------------- processing
    def process_run(self, key: str, needs: ModelNeeds, init: datetime, levels: list[int],
                    progress: Callable[[int, int], None] | None = None) -> dict:
        m = self.cfg.resolved_models[key]
        pts = self.resolve_points(key, m, needs, init)
        pkeys = list(pts)
        indices = [pts[k]["index"] for k in pkeys]
        files = list(self.required_files(m, needs, init, levels))
        g = self.cfg.general

        results: dict[str, tuple] = {}
        total_bytes = 0
        t0 = time.time()
        with ProcessPoolExecutor(max_workers=g.download_workers) as ex:
            futs = {}
            for h, role, lv, name in files:
                url = fmt(m.directory_template, init, h) + name
                futs[ex.submit(_extract_job, url, indices, str(self.tmp_dir), g.user_agent,
                               g.http_timeout_seconds, g.http_retries)] = (h, role, lv)
            done = 0
            missing: list[str] = []
            allowed = int(len(futs) * g.max_missing_fraction)
            for fut in as_completed(futs):
                h, role, lv = futs[fut]
                url, vals, units, size = fut.result()
                if vals is None:
                    # The Datamart occasionally omits single files (e.g. one GDPS level at
                    # one hour).  Tolerate a few; the affected level/time becomes a gap.
                    missing.append(url)
                    if len(missing) > allowed:
                        for f in futs:
                            f.cancel()
                        raise RunIncomplete(f"{len(missing)} files missing, e.g. {url}")
                    vals, units = [None] * len(indices), ""
                results[(h, role, lv)] = (vals, units)
                total_bytes += size
                done += 1
                if progress and (done % 50 == 0 or done == len(futs)):
                    progress(done, len(futs))
        log.info("%s %s: %d files (%d missing), %.0f MB in %.0fs", key, run_id(init), len(files),
                 len(missing), total_bytes / 1e6, time.time() - t0)
        for u in missing[:10]:
            log.warning("missing on Datamart: %s", u)

        # ---- assemble per point
        out = {"model": key, "run": run_id(init), "levels": levels, "signature": needs.signature(),
               "processed_at": datetime.now(UTC).isoformat(), "bytes": total_bytes,
               "missing_files": len(missing),
               "points": pts, "profile": None, "precip": None}
        if needs.need_profile:
            hours = profile_hours(m, needs.max_hour)
            prof = {"hours": hours, "data": {}}
            for pi, pk in enumerate(pkeys):
                d = {"surface_pressure_hpa": []}
                for role in needs.profile_roles:
                    d[role] = []
                for h in hours:
                    v, u = results[(h, "surface_pressure", None)]
                    d["surface_pressure_hpa"].append(_to_hpa(v[pi], u))
                    for role in needs.profile_roles:
                        row = []
                        for lv in levels:
                            v, u = results[(h, role, lv)]
                            row.append(_convert(role, v[pi], u))
                        d[role].append(row)
                prof["data"][pk] = d
            out["profile"] = prof
        if needs.need_precip:
            hours = precip_hours(m, needs.max_hour)
            pr = {"hours": [0] + hours, "data": {}}
            for pi, pk in enumerate(pkeys):
                series = [0.0]
                for h in hours:
                    v, u = results[(h, "precip_accum", None)]
                    series.append(_convert("precip_accum", v[pi], u))
                pr["data"][pk] = series
            out["precip"] = pr
        return out


def _to_hpa(v, units):
    if v is None:
        return None
    return v / 100.0 if (units == "Pa" or v > 2000) else v


def _convert(role: str, v, units: str):
    if v is None:
        return None
    if role == "temperature":
        return v - 273.15 if (units == "K" or v > 150) else v          # -> °C
    if role == "precip_accum":
        return v * 1000.0 if units == "m" else v                         # -> mm
    if role == "wind_speed":
        if units in ("knots", "kt"):
            return v * 1.852
        return v * 3.6                                                   # m/s -> km/h
    return v                                                              # height m, depression K/°C, direction deg
