"""Requirements, neighbourhood geometry and run assembly (no network)."""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml

from skiforecast import grib
from skiforecast.config import load_config
from skiforecast.datamart import (ModelNeeds, PointReq, assemble_run, compute_needs, extraction_indices,
                                  point_entries, precip_hours, profile_hours)
from skiforecast.store import Store

UTC = timezone.utc
STEP = 0.0225


def make_cfg(tmp_path: Path, extra_charts=()):
    src = Path(__file__).resolve().parents[1] / "config" / "config.example.yaml"
    raw = yaml.safe_load(src.read_text())
    raw["general"]["data_dir"] = str(tmp_path)
    raw["discord"]["enabled"] = False
    raw["discord"]["default_webhook"] = None
    raw["discord"]["alerts_webhook"] = None
    # only the meteograms from the example, so each test controls the blended charts
    raw["charts"] = [c for c in raw["charts"] if c.get("type", "meteogram") == "meteogram"] + list(extra_charts)
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(raw))
    return load_config(p)


SNOW = {"type": "blended_snow", "id": "snow", "title": "t",
        "sources": [{"model": "hrdps", "point": "whistler_peak"}], "elevations": [2200, 1600]}


def make_grid(nj=20, ni=20, lat0=50.0, lon0=-123.0):
    lats = lat0 + STEP * np.arange(nj)[:, None] * np.ones((1, ni))
    lons = lon0 + STEP * np.arange(ni)[None, :] * np.ones((nj, 1))
    return grib.GridInfo("test", ni, nj, lats, lons)


# --------------------------------------------------------------------------- needs
def test_compute_needs_blended_snow(tmp_path):
    base = compute_needs(make_cfg(tmp_path))
    needs = compute_needs(make_cfg(tmp_path, [SNOW]))
    hr = needs["hrdps"]
    assert "dewpoint_depression" in hr.profile_roles and {"temperature", "height"} <= hr.profile_roles
    assert hr.need_precip and hr.need_profile
    assert hr.points["whistler_peak@0,0"].radius_km == 6.0
    assert all(p.radius_km == 0.0 for k, p in hr.points.items() if k != "whistler_peak@0,0")
    assert all(p.radius_km == 0.0 for p in needs["gdps"].points.values())
    # the radius is part of the signature, so adding the chart reprocesses hrdps but not gdps
    assert hr.signature() != base["hrdps"].signature()
    assert needs["gdps"].signature() == base["gdps"].signature()


def test_wind_roles_only_with_use_wind(tmp_path):
    snow = dict(SNOW, sources=[{"model": "gdps", "point": "whistler_village"}])
    n = compute_needs(make_cfg(tmp_path, [snow]))["gdps"]
    assert n.points["whistler_village@0,0"].radius_km == 6.0
    snow_w = dict(snow, blend={"use_wind": True, "neighbourhood_km": 10})
    n = compute_needs(make_cfg(tmp_path, [snow_w]))["gdps"]
    assert {"wind_speed", "wind_direction"} <= n.profile_roles
    assert n.points["whistler_village@0,0"].radius_km == 10.0


# --------------------------------------------------------------------------- geometry
def test_grid_spacing_km():
    g = make_grid()
    km = 6371.0 * math.pi / 180.0
    dx = STEP * km * math.cos(math.radians(50.0 + 5 * STEP))
    dy = STEP * km
    assert abs(grib.grid_spacing_km(g, 5, 5) - (dx + dy) / 2) < 0.01
    # far edges use the previous cell
    dx_top = STEP * km * math.cos(math.radians(50.0 + 19 * STEP))
    assert abs(grib.grid_spacing_km(g, 19, 19) - (dx_top + dy) / 2) < 0.01


def test_cell_block():
    g = make_grid()
    blk = grib.cell_block(g, 10, 10, 2)
    assert blk[0] == (0, 0, 10 * 20 + 10)
    assert len(blk) == 25 and len({b[2] for b in blk}) == 25
    assert (-2, 1, 8 * 20 + 11) in blk
    corner = grib.cell_block(g, 0, 19, 3)
    assert corner[0] == (0, 0, 19)
    assert len(corner) == 16
    assert all(dj >= 0 and di <= 0 for dj, di, _ in corner)
    assert grib.cell_block(g, 4, 4, 0) == [(0, 0, 84)]


def test_point_entries_radius():
    g = make_grid()
    needs = ModelNeeds("m")
    needs.points["a@0,0"] = PointReq("a@0,0", "a", 50.2, -122.8, (0, 0), 6.0)
    needs.points["b@0,0"] = PointReq("b@0,0", "b", 50.2, -122.8, (0, 0))
    needs.points["c@0,0"] = PointReq("c@0,0", "c", 50.2, -122.8, (0, 0), 50.0)
    pts = point_entries(g, needs)
    a = pts["a@0,0"]
    assert a["radius_cells"] == round(6.0 / a["spacing_km"]) == 3
    assert a["cells"][0] == [0, 0, a["index"]] and len(a["cells"]) == 49
    assert pts["b@0,0"]["radius_cells"] == 0 and pts["b@0,0"]["cells"] == [[0, 0, a["index"]]]
    assert pts["c@0,0"]["radius_cells"] == 3                     # capped
    assert extraction_indices(pts) == sorted({c[2] for c in a["cells"]})


# --------------------------------------------------------------------------- assembly
def test_assemble_run_format(tmp_path):
    cfg = make_cfg(tmp_path)
    m = cfg.resolved_models["hrdps"]
    g = make_grid()
    needs = ModelNeeds("hrdps", need_profile=True, need_precip=True, max_hour=6,
                       profile_roles={"temperature", "height", "dewpoint_depression"})
    needs.points["p@0,0"] = PointReq("p@0,0", "p", 50.1, -122.9, (0, 0), 2.5)   # radius 1 cell
    needs.points["q@0,0"] = PointReq("q@0,0", "q", 50.3, -122.7, (0, 0))
    pts = point_entries(g, needs)
    assert pts["p@0,0"]["radius_cells"] == 1
    indices = extraction_indices(pts)
    levels = [1000, 850, 700]
    init = datetime(2026, 11, 6, 0, tzinfo=UTC)

    # value = flat index (+ hour / level offsets) so every number is traceable
    results = {}
    for h in profile_hours(m, 6):
        results[(h, "surface_pressure", None)] = ([90000.0 + i for i in indices], "Pa")
        for lv in levels:
            results[(h, "temperature", lv)] = ([273.15 + i for i in indices], "K")
            results[(h, "height", lv)] = ([float(i + lv) for i in indices], "m")
            results[(h, "dewpoint_depression", lv)] = ([1.0 * h for _ in indices], "K")
    for h in precip_hours(m, 6):
        results[(h, "precip_accum", None)] = ([0.001 * h * i for i in indices], "m")

    run = assemble_run("hrdps", m, needs, init, levels, pts, indices, results, 123, 1)
    run = json.loads(json.dumps(run))       # must be JSON-serialisable as stored
    assert set(run) == {"model", "run", "levels", "signature", "processed_at", "bytes", "missing_files",
                        "points", "profile", "precip"}
    assert run["run"] == "20261106T00Z" and run["bytes"] == 123 and run["missing_files"] == 1
    p = run["points"]["p@0,0"]
    centre = p["index"]
    prof = run["profile"]
    assert prof["hours"] == profile_hours(m, 6)
    d = prof["data"]["p@0,0"]
    assert set(d) == {"surface_pressure_hpa", "temperature", "height", "dewpoint_depression"}
    assert d["surface_pressure_hpa"][0] == (90000.0 + centre) / 100
    assert len(d["temperature"]) == len(prof["hours"]) and len(d["temperature"][0]) == 3
    assert abs(d["temperature"][0][0] - centre) < 1e-9
    nb = prof["neighbours"]["p@0,0"]
    assert len(nb) == 8 and "0,0" not in nb
    up_left = (p["j"] - 1) * g.ni + p["i"] - 1
    assert abs(nb["-1,-1"]["temperature"][1][2] - up_left) < 1e-9
    assert prof["neighbours"]["q@0,0"] == {}
    pr = run["precip"]
    assert pr["hours"] == [0] + precip_hours(m, 6)
    assert pr["data"]["p@0,0"][0] == 0.0
    assert abs(pr["data"]["p@0,0"][-1] - 6 * centre) < 1e-6
    right = p["j"] * g.ni + p["i"] + 1
    assert abs(pr["neighbours"]["p@0,0"]["0,1"][-1] - 6 * right) < 1e-6
    assert pr["neighbours"]["q@0,0"] == {}


def test_assemble_run_without_profile(tmp_path):
    cfg = make_cfg(tmp_path)
    m = cfg.resolved_models["hrdps"]
    needs = ModelNeeds("hrdps", need_precip=True, max_hour=3)
    needs.points["q@0,0"] = PointReq("q@0,0", "q", 50.3, -122.7, (0, 0))
    pts = {"q@0,0": {"index": 7}}                      # old-style entry without "cells"
    results = {(h, "precip_accum", None): ([2.0], "mm") for h in precip_hours(m, 3)}
    run = assemble_run("hrdps", m, needs, datetime(2026, 11, 6, tzinfo=UTC), [0], pts, [7], results)
    assert run["profile"] is None
    assert run["precip"]["data"]["q@0,0"][1:] == [2.0] * len(precip_hours(m, 3))
    assert run["precip"]["neighbours"] == {"q@0,0": {}}


# --------------------------------------------------------------------------- store
def test_recent_runs(tmp_path):
    st = Store(tmp_path)
    for rid in ["20261105T12Z", "20261106T00Z", "20261105T18Z", "20261106T06Z"]:
        st.save_run("hrdps", {"run": rid}, keep=3)
    assert [r["run"] for r in st.recent_runs("hrdps", 5)] == ["20261106T06Z", "20261106T00Z", "20261105T18Z"]
    assert [r["run"] for r in st.recent_runs("hrdps", 2)] == ["20261106T06Z", "20261106T00Z"]
    assert st.latest_run("hrdps")["run"] == "20261106T06Z"
    assert st.recent_runs("gdps", 3) == []
