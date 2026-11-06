import math

import numpy as np

from skiforecast import meteo

LEVELS = [1015, 1000, 950, 900, 850, 800, 750, 700, 650, 600]
# standard-ish heights (m) for those levels
HEIGHTS = [0, 110, 540, 990, 1460, 1950, 2470, 3010, 3590, 4210]


def temps(fl_m, lapse=6.5):
    return [(fl_m - z) * lapse / 1000 for z in HEIGHTS]


def test_ground_height_interpolates():
    z = meteo.ground_height(LEVELS, HEIGHTS, 850)
    assert abs(z - 1460) < 1
    z = meteo.ground_height(LEVELS, HEIGHTS, 875)
    assert 990 < z < 1460


def test_freezing_level_simple():
    _, fl, _ = meteo.profile_diagnostics(LEVELS, temps(1800), HEIGHTS, None, 1013)
    assert abs(fl - 1800) < 5


def test_below_ground_modes():
    # freezing level at 600 m, model ground at 900 hPa (990 m)
    t = temps(600)
    zg, fl, _ = meteo.profile_diagnostics(LEVELS, t, HEIGHTS, None, 900, below_ground="extrapolate")
    assert abs(fl - 600) < 5
    zg, fl, _ = meteo.profile_diagnostics(LEVELS, t, HEIGHTS, None, 900, below_ground="clamp")
    assert abs(fl - zg) < 1 and abs(zg - 990) < 1
    _, fl, _ = meteo.profile_diagnostics(LEVELS, t, HEIGHTS, None, 900, below_ground="hide")
    assert fl is None


def test_highest_vs_lowest_with_warm_layer_aloft():
    # cold at the surface, warm nose 1500-2000 m, cold above 2300 m
    t = [-2, -2, -1, -0.5, 1.5, 2.0, -1, -4, -7, -10]
    _, hi, _ = meteo.profile_diagnostics(LEVELS, t, HEIGHTS, None, 1013, "highest")
    _, lo, _ = meteo.profile_diagnostics(LEVELS, t, HEIGHTS, None, 1013, "lowest")
    assert 1950 < hi < 2470
    assert lo == 0 or lo < 1000


def test_wetbulb_below_freezing_level_when_dry():
    t = temps(2000)
    depr = [5.0] * len(LEVELS)
    _, fl, wbz = meteo.profile_diagnostics(LEVELS, t, HEIGHTS, depr, 1013)
    assert wbz < fl - 200


def test_wetbulb_equals_temperature_when_saturated():
    tw = meteo.wet_bulb(np.array([5.0, -3.0]), np.array([0.0, 0.0]), np.array([900.0, 700.0]))
    assert np.allclose(tw, [5.0, -3.0], atol=0.05)


def test_wind_interpolation_and_direction():
    spd = [20] * len(LEVELS)
    dirn = [270] * len(LEVELS)
    s, d = meteo.wind_at_height(LEVELS, HEIGHTS, spd, dirn, 2200)
    assert abs(s - 20) < 1e-6 and abs(d - 270) < 1e-6
    dirn = [350] * 5 + [10] * 5
    s, d = meteo.wind_at_height(LEVELS, HEIGHTS, spd, dirn, (1460 + 1950) / 2)
    assert d < 20 or d > 340   # interpolates across north, not through south
