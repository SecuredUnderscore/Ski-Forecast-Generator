"""Snow physics for the Blended Snow Forecast.

Small, pure, numpy-vectorised building blocks; no I/O and no config.  The
blend (blend.py) evaluates them for many members at once.

Literature basis
----------------
* Rain/snow partition from the wet-bulb temperature: wet-bulb (or humidity-aware)
  thresholds separate rain and snow much better than air temperature, with the
  50 % point close to 0.5-1 °C Tw (Jennings, Winchell, Livneh & Molotch 2018,
  Nature Communications 9:1148).  Operational snow models such as Météo-France's
  SAFRAN/Crocus use a linear ramp over a ~1-1.5 K interval; we use a ramp from
  0 °C (all snow) to 1.5 °C (all rain).
* Snow-to-liquid ratio (SLR) from temperature: the climatological "10:1" is a poor
  fit; SLR peaks in the dendritic-growth range around -12...-18 °C and drops for
  warmer (riming, partial melting) and very cold (small plates/columns) snow
  (Dubé 2003, "From mm to cm", MSC; Baxter, Graves & Moore 2005, Nat. Wea. Dig. 29).
  The lookup below is a smoothed version of those curves.
* Wind: blowing snow fragments crystals and packs the deposit, lowering SLR
  (Alcott & Steenburgh 2010, Wea. Forecasting 25).  A simple linear reduction
  above 30 km/h captures the sign and rough size of the effect.
* Snow level below the melting level: melting cools the air, and with heavier
  precipitation the 0 °C isotherm and the snow level are pulled down further,
  most strongly over mountains where the air is forced to rise
  (Minder, Durran & Roe 2011, J. Atmos. Sci. 68).  We model the snow level as the
  wet-bulb 0 °C height minus an offset that doubles with the precipitation rate.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

# Snow-to-liquid ratio lookup (air temperature °C -> ratio), ascending temperature.
_SLR_T = np.array([-30.0, -22.0, -18.0, -15.0, -12.0, -9.0, -6.0, -3.0, 0.0, 2.0])
_SLR_R = np.array([12.0, 15.0, 18.0, 19.0, 18.0, 16.0, 14.0, 12.0, 9.0, 7.0])

SNOW_TW_C = 0.0        # at/below this wet-bulb temperature everything falls as snow
RAIN_TW_C = 1.5        # at/above this everything falls as rain


def value_at_height(z_levels, values, z):
    """Linearly interpolate a profile quantity to height(s) ``z`` (m).

    Beyond the lowest/highest level the profile is extrapolated linearly from the
    two nearest levels (the "extrapolate" below-ground policy: an elevation under
    the model's smoothed terrain still gets a sensible value).  Levels where the
    height or value is None/NaN are ignored.  Returns a float for scalar ``z``,
    otherwise an array shaped like ``z``; NaN if fewer than two valid levels.
    """
    zl = np.asarray(z_levels, dtype=float).ravel()
    vl = np.asarray(values, dtype=float).ravel()
    zq = np.asarray(z, dtype=float)
    ok = np.isfinite(zl) & np.isfinite(vl)
    zl, vl = zl[ok], vl[ok]
    if len(zl) < 2:
        out = np.full(zq.shape, np.nan)
        return float(out) if out.ndim == 0 else out
    order = np.argsort(zl, kind="stable")
    zl, vl = zl[order], vl[order]
    out = np.interp(zq, zl, vl)
    lo_slope = (vl[1] - vl[0]) / (zl[1] - zl[0]) if zl[1] != zl[0] else 0.0
    hi_slope = (vl[-1] - vl[-2]) / (zl[-1] - zl[-2]) if zl[-1] != zl[-2] else 0.0
    out = np.where(zq < zl[0], vl[0] + lo_slope * (zq - zl[0]), out)
    out = np.where(zq > zl[-1], vl[-1] + hi_slope * (zq - zl[-1]), out)
    return float(out) if np.ndim(out) == 0 else out


def snow_fraction(tw_c):
    """Share (0..1) of the precipitation falling as snow, from the wet-bulb temperature.

    1 at Tw <= 0 °C, 0 at Tw >= 1.5 °C, linear in between (Jennings et al. 2018;
    SAFRAN-style ramp).  NaN in -> NaN out.
    """
    tw = np.asarray(tw_c, dtype=float)
    out = np.clip((RAIN_TW_C - tw) / (RAIN_TW_C - SNOW_TW_C), 0.0, 1.0)
    return float(out) if out.ndim == 0 else out


def snow_ratio(t_c, wind_kmh=None):
    """Snow-to-liquid ratio (cm of snow per cm of water) from air temperature at the elevation.

    Piecewise-linear in the spirit of Dubé (2003) / Baxter et al. (2005): ~7 near +2 °C,
    9 at 0 °C, peaking at 19 around -15 °C (dendrites), decreasing again in very cold air;
    flat outside -30...+2 °C.  With ``wind_kmh`` the ratio is reduced linearly from
    x1.0 at <= 30 km/h to x0.6 at >= 70 km/h (Alcott & Steenburgh 2010).
    """
    t = np.asarray(t_c, dtype=float)
    r = np.interp(t, _SLR_T, _SLR_R)
    r = np.where(np.isfinite(t), r, np.nan)
    if wind_kmh is not None:
        w = np.asarray(wind_kmh, dtype=float)
        factor = 1.0 - 0.4 * np.clip((w - 30.0) / 40.0, 0.0, 1.0)
        r = r * np.where(np.isfinite(factor), factor, 1.0)   # unknown wind -> no reduction
    return float(r) if np.ndim(r) == 0 else r


def snow_level_offset_m(rate_mm_h, base_m: float):
    """How far (m) snow reaches below the wet-bulb 0 °C height.

    ``base_m`` in light precipitation, growing linearly to ``2 * base_m`` at >= 2 mm/h
    (melting-induced cooling and orographic lowering; Minder, Durran & Roe 2011).
    """
    rate = np.asarray(rate_mm_h, dtype=float)
    rate = np.where(np.isfinite(rate), rate, 0.0)
    out = base_m + base_m * np.clip(rate / 2.0, 0.0, 1.0)
    return float(out) if np.ndim(out) == 0 else out


def weighted_quantiles(values, weights, qs):
    """Weighted quantiles of ``values`` (NaN values and non-positive weights ignored).

    Uses the midpoint ("Hazen") plotting position: sorted value i sits at cumulative
    probability (W_i - w_i/2) / W, linear in between and clamped at the ends.  With
    equal weights this equals ``np.quantile(values, qs, method="hazen")``.
    Returns a float for scalar ``qs``, else an array; NaN if no valid values.
    """
    v = np.asarray(values, dtype=float).ravel()
    w = np.broadcast_to(np.asarray(weights, dtype=float), np.shape(values)).ravel()
    q = np.asarray(qs, dtype=float)
    ok = np.isfinite(v) & np.isfinite(w) & (w > 0)
    v, w = v[ok], w[ok]
    if len(v) == 0:
        out = np.full(q.shape, np.nan)
    else:
        order = np.argsort(v, kind="stable")
        v, w = v[order], w[order]
        cw = np.cumsum(w)
        pos = (cw - 0.5 * w) / cw[-1]
        out = np.interp(q, pos, v)
    return float(out) if np.ndim(out) == 0 else out


def weighted_mean(values, weights) -> Optional[float]:
    """Weighted mean ignoring NaN values; None if nothing valid."""
    v = np.asarray(values, dtype=float).ravel()
    w = np.broadcast_to(np.asarray(weights, dtype=float), np.shape(values)).ravel()
    ok = np.isfinite(v) & np.isfinite(w) & (w > 0)
    if not ok.any():
        return None
    return float(np.sum(v[ok] * w[ok]) / np.sum(w[ok]))
