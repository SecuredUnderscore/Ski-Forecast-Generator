"""Config parsing for the two chart types and the posting options."""
from pathlib import Path

import pytest
import yaml

from skiforecast.config import ChartConfig, ConfigError, SnowChartConfig, load_config

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"


def raw_example(tmp_path: Path) -> dict:
    raw = yaml.safe_load(EXAMPLE.read_text())
    raw["general"]["data_dir"] = str(tmp_path)
    raw["discord"]["enabled"] = False
    raw["discord"]["default_webhook"] = None
    raw["discord"]["alerts_webhook"] = None
    return raw


def load(tmp_path: Path, raw: dict):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(raw))
    return load_config(p)


def test_example_config_has_both_chart_types(tmp_path):
    cfg = load(tmp_path, raw_example(tmp_path))
    kinds = {c.id: type(c) for c in cfg.charts}
    assert kinds["long_term"] is ChartConfig          # no `type:` -> meteogram
    assert kinds["snow_2day"] is SnowChartConfig
    snow = next(c for c in cfg.charts if c.id == "snow_2day")
    assert [e.elevation for e in snow.elevations] == [2182, 1850, 675]   # highest first
    assert snow.models() == ["hrdps"] and snow.post.models == ["hrdps"]
    assert "hrdps" in cfg.used_models()


def test_bare_elevations_and_post_times(tmp_path):
    raw = raw_example(tmp_path)
    raw["charts"].append({"type": "blended_snow", "id": "s", "title": "t",
                          "sources": [{"model": "hrdps", "point": "whistler_peak"}],
                          "elevations": [1100, 2200, 1600],
                          "post": {"times": ["6:00", "17:30", "06:00"]}})
    cfg = load(tmp_path, raw)
    c = next(c for c in cfg.charts if c.id == "s")
    assert [e.elevation for e in c.elevations] == [2200, 1600, 1100]
    assert c.post.times == ["06:00", "17:30"] and c.post.scheduled


@pytest.mark.parametrize("patch, needle", [
    ({"post": {"times": ["25:00"]}}, "HH:MM"),
    ({"post": {"models": ["gdps"]}}, "not used by this chart"),
    ({"blend": {"lagged_runs": 5}}, "keep_runs"),
    ({"elevations": []}, "1 to 5"),
    ({"bar_hours": 5}, "multiple of bar_hours"),
])
def test_blended_chart_validation_errors(tmp_path, patch, needle):
    raw = raw_example(tmp_path)
    chart = {"type": "blended_snow", "id": "s", "title": "t",
             "sources": [{"model": "hrdps", "point": "whistler_peak"}], "elevations": [2000]}
    chart.update(patch)
    raw["charts"].append(chart)
    with pytest.raises(ConfigError) as e:
        load(tmp_path, raw)
    assert needle in str(e.value)


def test_meteogram_post_models_must_be_used(tmp_path):
    raw = raw_example(tmp_path)
    raw["charts"][0]["post"] = {"models": ["gdps"]}      # low_2day uses hrdps_west + hrdps only
    with pytest.raises(ConfigError) as e:
        load(tmp_path, raw)
    assert "post.models" in str(e.value)
