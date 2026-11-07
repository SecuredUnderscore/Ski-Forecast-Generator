"""GRIB2 decoding helpers (ecCodes): grid geometry and point extraction."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

try:  # ecCodes is only needed for real downloads; tests and rendering work without it
    import eccodes
except ImportError:  # pragma: no cover
    eccodes = None  # type: ignore[assignment]


@dataclass
class GridInfo:
    signature: str
    ni: int
    nj: int
    lats: np.ndarray      # shape (nj, ni), geographic
    lons: np.ndarray      # shape (nj, ni), -180..180


def _open_first(path: str):
    if eccodes is None:
        raise RuntimeError("the eccodes package is required to decode GRIB files")
    f = open(path, "rb")
    gid = eccodes.codes_grib_new_from_file(f)
    if gid is None:
        f.close()
        raise ValueError(f"no GRIB message in {path}")
    return f, gid


def read_grid(path: str) -> GridInfo:
    f, gid = _open_first(path)
    try:
        ni = eccodes.codes_get(gid, "Ni")
        nj = eccodes.codes_get(gid, "Nj")
        grid_type = eccodes.codes_get(gid, "gridType")
        # ecCodes returns geographic (un-rotated) coordinates for rotated grids too.
        lats = eccodes.codes_get_double_array(gid, "latitudes").reshape(nj, ni)
        lons = eccodes.codes_get_double_array(gid, "longitudes").reshape(nj, ni)
        lons = ((lons + 180.0) % 360.0) - 180.0
        sig = f"{grid_type}:{ni}x{nj}:{lats[0, 0]:.4f},{lons[0, 0]:.4f}:{lats[-1, -1]:.4f},{lons[-1, -1]:.4f}"
        return GridInfo(sig, ni, nj, lats, lons)
    finally:
        eccodes.codes_release(gid)
        f.close()


def nearest_index(grid: GridInfo, lat: float, lon: float, offset: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """Return (j, i) of the grid cell nearest to lat/lon, shifted by offset.

    offset = (east, north) in grid cells.  Directions are resolved from the
    local grid orientation so "1 left, 1 down" means what it looks like on a map
    regardless of the file's scanning order.
    """
    lon = ((lon + 180.0) % 360.0) - 180.0
    coslat = np.cos(np.radians(lat))
    dlon = ((grid.lons - lon + 180.0) % 360.0) - 180.0
    d2 = (grid.lats - lat) ** 2 + (dlon * coslat) ** 2
    j, i = np.unravel_index(int(np.argmin(d2)), d2.shape)
    dx, dy = offset
    if dx or dy:
        jn = min(j + 1, grid.nj - 1) if j + 1 < grid.nj else j - 1
        in_ = min(i + 1, grid.ni - 1) if i + 1 < grid.ni else i - 1
        # sign of +j / +i in the north / east direction
        north_sign = 1 if (grid.lats[jn, i] - grid.lats[j, i]) * (jn - j) > 0 else -1
        dlon_i = ((grid.lons[j, in_] - grid.lons[j, i] + 180) % 360) - 180
        east_sign = 1 if dlon_i * (in_ - i) > 0 else -1
        j = int(np.clip(j + dy * north_sign, 0, grid.nj - 1))
        i = int(np.clip(i + dx * east_sign, 0, grid.ni - 1))
    return int(j), int(i)


def extract_values(path: str, flat_indices: list[int]) -> tuple[list[float | None], str]:
    """Decode the first message of a GRIB file and return values at flat indices + units."""
    f, gid = _open_first(path)
    try:
        values = eccodes.codes_get_values(gid)
        missing = eccodes.codes_get_double(gid, "missingValue")
        try:
            units = eccodes.codes_get(gid, "units")
        except eccodes.KeyValueNotFoundError:
            units = ""
        out = []
        for k in flat_indices:
            v = float(values[k])
            out.append(None if (v == missing or not np.isfinite(v)) else v)
        return out, str(units)
    finally:
        eccodes.codes_release(gid)
        f.close()


_KM_PER_DEG = 111.195   # mean Earth radius 6371 km


def _dist_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlon = ((lon2 - lon1 + 180.0) % 360.0) - 180.0
    coslat = np.cos(np.radians((lat1 + lat2) / 2.0))
    return float(np.hypot((lat2 - lat1) * _KM_PER_DEG, dlon * coslat * _KM_PER_DEG))


def grid_spacing_km(grid: GridInfo, j: int, i: int) -> float:
    """Approximate grid spacing (km) at cell (j, i): the mean distance to the
    next cell along i and along j (the previous cell at the grid's far edges)."""
    dists = []
    if grid.ni > 1:
        i2 = i + 1 if i + 1 < grid.ni else i - 1
        dists.append(_dist_km(grid.lats[j, i], grid.lons[j, i], grid.lats[j, i2], grid.lons[j, i2]))
    if grid.nj > 1:
        j2 = j + 1 if j + 1 < grid.nj else j - 1
        dists.append(_dist_km(grid.lats[j, i], grid.lons[j, i], grid.lats[j2, i], grid.lons[j2, i]))
    return float(np.mean(dists)) if dists else 0.0


def cell_block(grid: GridInfo, j: int, i: int, radius_cells: int) -> list[tuple[int, int, int]]:
    """Cells within ``radius_cells`` of (j, i) in both directions, as
    (dj, di, flat_index).  The centre (0, 0) comes first; cells that would fall
    off the grid are skipped."""
    r = max(0, int(radius_cells))
    out = [(0, 0, j * grid.ni + i)]
    for dj in range(-r, r + 1):
        for di in range(-r, r + 1):
            if dj == 0 and di == 0:
                continue
            jj, ii = j + dj, i + di
            if 0 <= jj < grid.nj and 0 <= ii < grid.ni:
                out.append((dj, di, jj * grid.ni + ii))
    return out
