"""Vertical-profile diagnostics: model ground height, freezing level,
wet-bulb zero height and wind at a given elevation."""
from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np


def _clean(levels, *cols):
    """Drop levels where any column is missing; return sorted-by-pressure-desc arrays."""
    rows = [r for r in zip(levels, *cols) if all(v is not None for v in r)]
    rows.sort(key=lambda r: -r[0])
    if not rows:
        return [np.array([]) for _ in range(len(cols) + 1)]
    arr = np.array(rows, dtype=float)
    return [arr[:, k] for k in range(arr.shape[1])]


def ground_height(levels: Sequence[int], heights: Sequence[Optional[float]],
                  surface_pressure_hpa: Optional[float]) -> Optional[float]:
    """Model terrain height (m) from surface pressure, interpolated in ln(p)."""
    p, z = _clean(levels, heights)
    if surface_pressure_hpa is None or len(p) < 2:
        return None
    lp, lps = np.log(p), math.log(surface_pressure_hpa)
    # p descending -> ln p descending; np.interp needs ascending x
    if lps >= lp[0]:      # below the lowest level: extrapolate with the bottom two levels
        k0, k1 = 0, 1
    elif lps <= lp[-1]:
        k0, k1 = -2, -1
    else:
        return float(np.interp(lps, lp[::-1], z[::-1]))
    slope = (z[k1] - z[k0]) / (lp[k1] - lp[k0])
    return float(z[k0] + slope * (lps - lp[k0]))


def _above_ground(p, z, val, psfc, zsfc):
    mask = np.ones_like(p, dtype=bool)
    if psfc is not None:
        mask &= p <= psfc
    if zsfc is not None:
        mask &= z >= zsfc
    return z[mask], val[mask]


def zero_crossing_height(z: np.ndarray, t: np.ndarray, ground: Optional[float],
                         method: str = "highest") -> Optional[float]:
    """Height where the temperature profile crosses 0 °C.

    highest: top of the highest above-freezing layer (above it everything is frozen)
    lowest : first crossing above the ground
    Returns ground height if the whole column is below freezing, None if no
    crossing was found below the top level (whole column above freezing).
    """
    if len(z) == 0:
        return None
    order = np.argsort(z)
    z, t = z[order], t[order]
    crossings = []
    for k in range(len(z) - 1):
        t0, t1 = t[k], t[k + 1]
        if t0 > 0 >= t1 or t0 <= 0 < t1:
            if t0 == t1:
                continue
            crossings.append((z[k] + (z[k + 1] - z[k]) * (t0 / (t0 - t1)), t0 > 0))
    warm_to_cold = [c for c, w in crossings if w]
    if method == "lowest":
        if t[0] <= 0:
            return float(ground if ground is not None else z[0])
        return float(warm_to_cold[0]) if warm_to_cold else None
    if warm_to_cold:
        return float(warm_to_cold[-1])
    if t[-1] <= 0 and np.all(t <= 0):
        return float(ground if ground is not None else z[0])
    if t[-1] > 0:
        return None
    return float(ground if ground is not None else z[0])


def wet_bulb(t_c: np.ndarray, depr: np.ndarray, p_hpa: np.ndarray) -> np.ndarray:
    """Wet-bulb temperature (°C) from the psychrometric equation, solved by bisection."""
    td = t_c - depr
    es = lambda x: 6.112 * np.exp(17.67 * x / (x + 243.5))
    e = es(td)
    lo, hi = td.copy(), t_c.copy()
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        gamma = 6.53e-4 * (1 + 0.000944 * mid) * p_hpa
        f = es(mid) - gamma * (t_c - mid) - e
        hi = np.where(f > 0, mid, hi)
        lo = np.where(f > 0, lo, mid)
    return 0.5 * (lo + hi)


def _level_height(p, z, val, psfc, zsfc, method, below_ground):
    """Freezing height for one profile honouring the below-ground policy.

    extrapolate: also use the model's (extrapolated) pressure levels below its
                 terrain, so a freezing level under a smoothed mountain is still
                 estimated (like a valley would see it)
    clamp      : only above-ground levels; an all-frozen column returns ground height
    hide       : like clamp, but an all-frozen column returns None
    """
    if below_ground == "extrapolate" and method == "highest":
        return zero_crossing_height(z, val, float(np.min(z)) if len(z) else None, method)
    zz, vv = _above_ground(p, z, val, psfc, zsfc)
    h = zero_crossing_height(zz, vv, zsfc, method)
    if below_ground == "hide" and h is not None and zsfc is not None and abs(h - zsfc) < 1e-6:
        return None
    return h


def profile_diagnostics(levels, temp, height, depr, psfc, method="highest", below_ground="extrapolate"):
    """Return (ground_m, freezing_level_m, wetbulb_zero_m) for one time step."""
    zsfc = ground_height(levels, height, psfc)
    p, z, t = _clean(levels, height, temp)
    if len(p) < 2:
        return zsfc, None, None
    fl = _level_height(p, z, t, psfc, zsfc, method, below_ground)
    wbz = None
    if depr is not None:
        p2, z2, t2, d2 = _clean(levels, height, temp, depr)
        if len(p2) >= 2:
            tw = wet_bulb(t2, d2, p2)
            wbz = _level_height(p2, z2, tw, psfc, zsfc, method, below_ground)
    return zsfc, fl, wbz


def wind_at_height(levels, height, speed, direction, target_m: float, psfc=None):
    """Interpolate wind (speed km/h, direction deg) to a height via u/v components."""
    p, z, s, d = _clean(levels, height, speed, direction)
    if len(p) < 2:
        return None, None
    if psfc is not None:
        m = p <= psfc
        p, z, s, d = p[m], z[m], s[m], d[m]
    if len(z) < 2 or target_m < z.min() or target_m > z.max():
        if len(z) == 0:
            return None, None
        k = int(np.argmin(np.abs(z - target_m)))      # clamp to nearest available level
        return float(s[k]), float(d[k])
    order = np.argsort(z)
    z, s, d = z[order], s[order], d[order]
    rad = np.radians(d)
    u, v = -s * np.sin(rad), -s * np.cos(rad)
    ui, vi = np.interp(target_m, z, u), np.interp(target_m, z, v)
    spd = float(math.hypot(ui, vi))
    dirn = float((math.degrees(math.atan2(-ui, -vi)) + 360) % 360)
    return spd, dirn
