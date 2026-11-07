"""Snow physics building blocks."""
import math

import numpy as np

from skiforecast.snow import (snow_fraction, snow_level_offset_m, snow_ratio, value_at_height,
                              weighted_quantiles)


def test_snow_fraction_endpoints_and_midpoint():
    assert snow_fraction(-5) == 1.0
    assert snow_fraction(0.0) == 1.0
    assert snow_fraction(1.5) == 0.0
    assert snow_fraction(4) == 0.0
    assert abs(snow_fraction(0.75) - 0.5) < 1e-9
    out = snow_fraction(np.array([-1.0, 0.75, 2.0, np.nan]))
    assert np.allclose(out[:3], [1, 0.5, 0]) and np.isnan(out[3])


def test_snow_ratio_shape_and_wind():
    temps = np.arange(0, -15.5, -0.5)
    r = snow_ratio(temps)
    assert np.all(np.diff(r) >= 0)                     # rises from 0 to -15 °C
    grid = np.arange(-35, 5, 0.5)
    assert abs(grid[int(np.argmax(snow_ratio(grid)))] + 15) <= 1.5   # peak near -15 °C
    assert snow_ratio(-40) == snow_ratio(-30) == 12    # flat outside the table
    assert snow_ratio(10) == snow_ratio(2) == 7
    assert snow_ratio(-10, 20) == snow_ratio(-10)
    assert snow_ratio(-10, 50) < snow_ratio(-10)
    assert abs(snow_ratio(-10, 100) - 0.6 * snow_ratio(-10)) < 1e-9


def test_value_at_height_interpolates_and_extrapolates():
    z = [3000, 1000, None, 2000]
    v = [-10.0, 3.0, 99.0, -4.0]
    assert abs(value_at_height(z, v, 1500) - (-0.5)) < 1e-9
    assert abs(value_at_height(z, v, 500) - 6.5) < 1e-9       # slope of the bottom two levels
    assert abs(value_at_height(z, v, 3500) - (-13.0)) < 1e-9  # slope of the top two levels
    out = value_at_height(z, v, np.array([1000.0, 2500.0]))
    assert np.allclose(out, [3.0, -7.0])
    assert math.isnan(value_at_height([1000, None], [1.0, 2.0], 1200))
    assert math.isnan(value_at_height([1000, 2000], [1.0, float("nan")], 1200))


def test_weighted_quantiles_matches_numpy_on_equal_weights():
    rng = np.random.default_rng(1)
    x = rng.normal(size=37)
    qs = [0.1, 0.5, 0.9]
    assert np.allclose(weighted_quantiles(x, np.ones_like(x), qs), np.quantile(x, qs, method="hazen"))
    xn = np.append(x, np.nan)
    assert np.allclose(weighted_quantiles(xn, 1.0, qs), np.quantile(x, qs, method="hazen"))
    assert math.isnan(weighted_quantiles([np.nan], [1.0], 0.5))
    # weight pulls the median
    assert weighted_quantiles([0.0, 10.0], [1.0, 9.0], 0.5) > 5


def test_snow_level_offset():
    assert snow_level_offset_m(0, 150) == 150
    assert snow_level_offset_m(1, 150) == 225
    assert snow_level_offset_m(2, 150) == 300
    assert snow_level_offset_m(10, 150) == 300
    assert np.allclose(snow_level_offset_m(np.array([0.0, 4.0]), 100), [100, 200])
