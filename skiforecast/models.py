"""Built-in Datamart model definitions.

Every model is described purely by data (URL + filename templates and variable
names), so new Datamart models can be added from config.yaml without code
changes.  Template placeholders:

    {date}   run date YYYYMMDD          {run}    run hour, 2 digits
    {hour}   forecast hour, 3 digits    {var}    variable name (from `variables`)
    {level}  pressure level hPa, 4 digits (profile templates only)

`profile_schedule` (optional) overrides `profile_step_hours` with
[[until_hour, step], ...] pairs for models whose output frequency changes.
"""
from __future__ import annotations

# Variable roles used by the pipeline:
#   temperature, height, dewpoint_depression, wind_speed, wind_direction  -> on pressure levels
#   surface_pressure, precip_accum                                         -> single level
STANDARD_LEVELS = [1015, 1000, 985, 970, 950, 925, 900, 875, 850, 800, 750, 700,
                   650, 600, 550, 500, 450, 400, 350, 300, 275, 250, 225, 200,
                   175, 150, 100, 50]

_DD = "https://dd.weather.gc.ca/{date}/WXO-DD"

BUILTIN_MODELS: dict[str, dict] = {
    "hrdps": {
        "label": "HRDPS",
        "description": "High Resolution Deterministic Prediction System, 2.5 km continental",
        "directory_template": _DD + "/model_hrdps/continental/2.5km/{run}/{hour}/",
        "profile_file_template": "{date}T{run}Z_MSC_HRDPS_{var}_ISBL_{level}_RLatLon0.0225_PT{hour}H.grib2",
        "surface_file_template": "{date}T{run}Z_MSC_HRDPS_{var}_RLatLon0.0225_PT{hour}H.grib2",
        "variables": {
            "temperature": "TMP",
            "height": "HGT",
            "dewpoint_depression": "DEPR",
            "wind_speed": "WIND",
            "wind_direction": "WDIR",
            "surface_pressure": "PRES_Sfc",
            "precip_accum": "APCP_Sfc",
        },
        "runs": [0, 6, 12, 18],
        "max_hour": 48,
        "profile_step_hours": 3,
        "precip_step_hours": 1,
        "levels": STANDARD_LEVELS,
        "typical_delay_hours": 3.0,
        "lookback_hours": 30,
        "stale_after_hours": 12,
    },
    "hrdps_west": {
        "label": "HRDPS-West 1km",
        "description": "Experimental HRDPS 1 km West (dd.alpha, 24 h history only)",
        "directory_template": "https://dd.alpha.weather.gc.ca/model_hrdps/west/1km/grib2/{run}/{hour}/",
        "profile_file_template": "CMC_hrdps_west_{var}_ISBL_{level}_rotated_latlon0.009x0.009_{date}T{run}Z_P{hour}-00.grib2",
        "surface_file_template": "CMC_hrdps_west_{var}_rotated_latlon0.009x0.009_{date}T{run}Z_P{hour}-00.grib2",
        "variables": {
            "temperature": "TMP",
            "height": "HGT",
            "dewpoint_depression": "DEPR",
            "wind_speed": "WIND",
            "wind_direction": "WDIR",
            "surface_pressure": "PRES_SFC_0",
            "precip_accum": "APCP_SFC_0",
        },
        "runs": [0, 12],
        "max_hour": 48,
        "profile_step_hours": 1,
        "precip_step_hours": 1,
        "levels": STANDARD_LEVELS,
        "typical_delay_hours": 5.5,
        "lookback_hours": 22,      # directory only keeps ~24 h
        "stale_after_hours": 20,
    },
    "rdps": {
        "label": "RDPS",
        "description": "Regional Deterministic Prediction System, 10 km",
        "directory_template": _DD + "/model_rdps/10km/{run}/{hour}/",
        "profile_file_template": "{date}T{run}Z_MSC_RDPS_{var}_IsbL-{level}_RLatLon0.09_PT{hour}H.grib2",
        "surface_file_template": "{date}T{run}Z_MSC_RDPS_{var}_RLatLon0.09_PT{hour}H.grib2",
        "variables": {
            "temperature": "AirTemp",
            "height": "GeopotentialHeight",
            "dewpoint_depression": "DewPointDepression",
            "wind_speed": "WindSpeed",
            "wind_direction": "WindDir",
            "surface_pressure": "Pressure_Sfc",
            "precip_accum": "Precip-Accum_Sfc",
        },
        "runs": [0, 6, 12, 18],
        "max_hour": 84,
        "profile_step_hours": 3,
        "precip_step_hours": 1,
        "levels": STANDARD_LEVELS,
        "typical_delay_hours": 3.0,
        "lookback_hours": 30,
        "stale_after_hours": 12,
    },
    "gdps": {
        "label": "GDPS",
        "description": "Global Deterministic Prediction System, 15 km",
        "directory_template": _DD + "/model_gdps/15km/{run}/{hour}/",
        "profile_file_template": "{date}T{run}Z_MSC_GDPS_{var}_IsbL-{level}_LatLon0.15_PT{hour}H.grib2",
        "surface_file_template": "{date}T{run}Z_MSC_GDPS_{var}_LatLon0.15_PT{hour}H.grib2",
        "variables": {
            "temperature": "AirTemp",
            "height": "GeopotentialHeight",
            "dewpoint_depression": "DewPointDepression",
            "wind_speed": "WindSpeed",
            "wind_direction": "WindDir",
            "surface_pressure": "Pressure_Sfc",
            "precip_accum": "Precip-Accum_Sfc",
        },
        "runs": [0, 12],
        "max_hour": 240,
        # Full upper-air profiles are published every 3 h to 168 h, then every 6 h.
        "profile_step_hours": 3,
        "profile_schedule": [[168, 3], [240, 6]],
        "precip_step_hours": 3,
        "levels": STANDARD_LEVELS,
        "typical_delay_hours": 3.5,
        "lookback_hours": 36,
        "stale_after_hours": 18,
    },
}
