"""MSC Datamart access: run discovery, completeness checks and point extraction.

A run is processed by downloading only the files we need, decoding the values
at the configured grid points and immediately deleting each file, so disk use
stays tiny even though the source grids are national/global.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import requests

from .config import AppConfig, ModelConfig, SnowChartConfig, SourceConfig
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


class DatamartBusy(Exception):
    """The Datamart kept timing out or dropping connections after all retries.

    Common for a few minutes after a run is published, when every consumer is
    downloading it at once; the service simply tries again on the next poll.
    """


def _backoff(attempt: int) -> float:
    # 5, 10, 20, 40, 60, ... s with jitter so parallel workers don't retry in lockstep.
    return min(5 * 2 ** attempt, 60) * random.uniform(0.75, 1.25)


class Http:
    def __init__(self, user_agent: str, timeout: float, retries: int):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = user_agent
        self.timeout = timeout
        self.retries = retries

    def _retry(self, url: str, fn):
        """Call ``fn()`` until it succeeds, retrying network errors and 5xx/429."""
        for attempt in range(self.retries + 1):
            try:
                return fn()
            except requests.RequestException as e:
                resp = getattr(e, "response", None)
                if resp is not None and resp.status_code < 500 and resp.status_code != 429:
                    raise  # e.g. 403: retrying won't help
                if attempt == self.retries:
                    raise DatamartBusy(f"{url}: {type(e).__name__}: {e}") from e
                log.debug("retrying %s after %s", url, e)
                time.sleep(_backoff(attempt))

    def get(self, url: str) -> requests.Response:
        """GET a small resource (fully read).  404 is returned, not raised."""
        def once():
            r = self.s.get(url, timeout=self.timeout)
            if r.status_code != 404:
                r.raise_for_status()
            return r
        return self._retry(url, once)

    def download(self, url: str, path: str) -> Optional[int]:
        """Stream ``url`` into ``path``; return the size, or None on 404.

        The whole transfer is retried, not just the request: a read timeout
        halfway through the body restarts the file from scratch.
        """
        def once():
            with self.s.get(url, timeout=self.timeout, stream=True) as r:
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                size = 0
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        size += len(chunk)
                return size
        return self._retry(url, once)

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
    radius_km: float = 0.0   # neighbourhood radius to extract around the cell (0 = centre only)


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
        points = sorted([k, float(p.radius_km)] for k, p in self.points.items())
        return json.dumps({"points": points, "roles": sorted(self.profile_roles),
                           "profile": self.need_profile, "precip": self.need_precip,
                           "max_hour": self.max_hour}, sort_keys=True)


def point_key(point: str, offset) -> str:
    return f"{point}@{offset[0]},{offset[1]}"


def _add_point(cfg: AppConfig, n: ModelNeeds, s: SourceConfig, radius_km: float = 0.0):
    """Register a source's point; the radius is the largest any chart asks for."""
    k = point_key(s.point, s.offset)
    old = n.points.get(k)
    if old is not None and old.radius_km >= radius_km:
        return
    p = cfg.points[s.point]
    n.points[k] = PointReq(k, s.point, p.lat, p.lon, tuple(s.offset), float(radius_km))


def compute_needs(cfg: AppConfig) -> dict[str, ModelNeeds]:
    needs: dict[str, ModelNeeds] = {}
    for c in cfg.enabled_charts():
        if isinstance(c, SnowChartConfig):
            for s in c.sources:
                m = cfg.resolved_models[s.model]
                n = needs.setdefault(s.model, ModelNeeds(s.model))
                _add_point(cfg, n, s, c.blend.neighbourhood_km)
                n.max_hour = max(n.max_hour, min(s.until_hour or m.max_hour, m.max_hour))
                n.need_profile = True
                n.profile_roles |= {"temperature", "height", "dewpoint_depression"}
                if c.blend.use_wind:
                    n.profile_roles |= {"wind_speed", "wind_direction"}
                n.need_precip = True
            continue
        layers = [("fl", c.freezing_level), ("precip", c.precip_sources)]
        for layer, sources in layers:
            for s in sources:
                m = cfg.resolved_models[s.model]
                n = needs.setdefault(s.model, ModelNeeds(s.model))
                _add_point(cfg, n, s)
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


MAX_RADIUS_CELLS = 3


def point_entries(g: "grib.GridInfo", needs: ModelNeeds) -> dict:
    """Grid cell(s) for every needed point: the centre cell plus, when the point
    asks for a neighbourhood, the block of cells around it."""
    out = {}
    for pk, p in needs.points.items():
        j, i = grib.nearest_index(g, p.lat, p.lon, p.offset)
        spacing = grib.grid_spacing_km(g, j, i)
        radius = 0
        if p.radius_km > 0 and spacing > 0:
            radius = min(MAX_RADIUS_CELLS, int(round(p.radius_km / spacing)))
        cells = [list(c) for c in grib.cell_block(g, j, i, radius)]
        out[pk] = {"point": p.point, "lat": p.lat, "lon": p.lon, "offset": list(p.offset),
                   "j": j, "i": i, "index": j * g.ni + i,
                   "grid_lat": round(float(g.lats[j, i]), 5), "grid_lon": round(float(g.lons[j, i]), 5),
                   "spacing_km": round(spacing, 3), "radius_cells": radius, "cells": cells}
    return out


def _point_cells(entry: dict) -> list[list[int]]:
    # grid caches / entries written before neighbourhoods existed only have "index"
    return entry.get("cells") or [[0, 0, entry["index"]]]


def extraction_indices(pts: dict) -> list[int]:
    """Unique, sorted flat indices of every cell of every point."""
    return sorted({c[2] for e in pts.values() for c in _point_cells(e)})


def _cell_profile(results: dict, pos: int, hours: list[int], levels: list[int], roles) -> dict:
    """Profile dict of one grid cell; ``pos`` is its position in the extracted values."""
    d = {"surface_pressure_hpa": []}
    for role in roles:
        d[role] = []
    for h in hours:
        v, u = results[(h, "surface_pressure", None)]
        d["surface_pressure_hpa"].append(_to_hpa(v[pos], u))
        for role in roles:
            row = []
            for lv in levels:
                v, u = results[(h, role, lv)]
                row.append(_convert(role, v[pos], u))
            d[role].append(row)
    return d


def _cell_precip(results: dict, pos: int, hours: list[int]) -> list:
    series = [0.0]
    for h in hours:
        v, u = results[(h, "precip_accum", None)]
        series.append(_convert("precip_accum", v[pos], u))
    return series


def assemble_run(key: str, m: ModelConfig, needs: ModelNeeds, init: datetime, levels: list[int],
                 pts: dict, indices: list[int], results: dict, total_bytes: int = 0,
                 missing: int = 0) -> dict:
    """Build the stored run dict from extracted values.

    ``results[(hour, role, level|None)] = (values, units)`` where ``values`` is
    aligned with ``indices`` (see :func:`extraction_indices`).
    """
    pos = {idx: n for n, idx in enumerate(indices)}
    out = {"model": key, "run": run_id(init), "levels": levels, "signature": needs.signature(),
           "processed_at": datetime.now(UTC).isoformat(), "bytes": total_bytes,
           "missing_files": missing,
           "points": pts, "profile": None, "precip": None}
    roles = sorted(needs.profile_roles)
    if needs.need_profile:
        hours = profile_hours(m, needs.max_hour)
        prof = {"hours": hours, "data": {}, "neighbours": {}}
        for pk, e in pts.items():
            cells = _point_cells(e)
            prof["data"][pk] = _cell_profile(results, pos[cells[0][2]], hours, levels, roles)
            prof["neighbours"][pk] = {f"{dj},{di}": _cell_profile(results, pos[idx], hours, levels, roles)
                                      for dj, di, idx in cells if (dj, di) != (0, 0)}
        out["profile"] = prof
    if needs.need_precip:
        hours = precip_hours(m, needs.max_hour)
        pr = {"hours": [0] + hours, "data": {}, "neighbours": {}}
        for pk, e in pts.items():
            cells = _point_cells(e)
            pr["data"][pk] = _cell_precip(results, pos[cells[0][2]], hours)
            pr["neighbours"][pk] = {f"{dj},{di}": _cell_precip(results, pos[idx], hours)
                                    for dj, di, idx in cells if (dj, di) != (0, 0)}
        out["precip"] = pr
    return out


# --------------------------------------------------------------------------- worker
def _extract_job(url: str, indices: list[int], tmp_dir: str, ua: str, timeout: float, retries: int):
    """Runs in a worker process: download one file, return values at indices."""
    http = Http(ua, timeout, retries)
    fd, path = tempfile.mkstemp(suffix=".grib2", dir=tmp_dir)
    os.close(fd)
    try:
        size = http.download(url, path)
        if size is None:
            return url, None, "", 0
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
        fd, path = tempfile.mkstemp(suffix=".grib2", dir=self.tmp_dir)
        os.close(fd)
        try:
            if self.http.download(url, path) is None:
                raise RunIncomplete(f"{url} missing")
            g = grib.read_grid(path)
        finally:
            os.remove(path)
        out = point_entries(g, needs)
        (self.grid_dir / f"{key}.json").write_text(json.dumps({"signature": g.signature, "points": out}, indent=1))
        return out

    # ------------------------------------------------------------- processing
    def process_run(self, key: str, needs: ModelNeeds, init: datetime, levels: list[int],
                    progress: Callable[[int, int], None] | None = None) -> dict:
        m = self.cfg.resolved_models[key]
        pts = self.resolve_points(key, m, needs, init)
        indices = extraction_indices(pts)
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
            try:
                for fut in as_completed(futs):
                    h, role, lv = futs[fut]
                    url, vals, units, size = fut.result()
                    if vals is None:
                        # The Datamart occasionally omits single files (e.g. one GDPS level at
                        # one hour).  Tolerate a few; the affected level/time becomes a gap.
                        missing.append(url)
                        if len(missing) > allowed:
                            raise RunIncomplete(f"{len(missing)} files missing, e.g. {url}")
                        vals, units = [None] * len(indices), ""
                    results[(h, role, lv)] = (vals, units)
                    total_bytes += size
                    done += 1
                    if progress and (done % 50 == 0 or done == len(futs)):
                        progress(done, len(futs))
            except BaseException:
                # Don't sit through hundreds of queued downloads before giving up.
                ex.shutdown(wait=True, cancel_futures=True)
                raise
        log.info("%s %s: %d files (%d missing), %.0f MB in %.0fs", key, run_id(init), len(files),
                 len(missing), total_bytes / 1e6, time.time() - t0)
        for u in missing[:10]:
            log.warning("missing on Datamart: %s", u)

        return assemble_run(key, m, needs, init, levels, pts, indices, results,
                            total_bytes, len(missing))

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
