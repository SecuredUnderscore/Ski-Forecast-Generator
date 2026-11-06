# Ski Forecast Generator

Automatic ski-forecast meteograms (freezing level, wet-bulb snow level, precipitation, optional ridge-top wind) built from Environment Canada's free [MSC Datamart](https://dd.weather.gc.ca/) model output, posted to Discord every time a model publishes a new run.
Inspired by PowderPicker, please support him on [Substack](https://powderpicker.substack.com/)!

![Example 10-day chart](docs/example_long_term.png)

## How it works

1. Every `poll_interval_minutes` (default 5), the service checks each model used by your charts for a new run.
2. A run counts as ready once every file the charts need for its first and last forecast hours has been published.
3. When a run is ready, the service downloads only the files it needs. That means the pressure levels from the surface up to `profile_top_hpa`, plus surface pressure and accumulated precipitation. It reads the values at your grid points and deletes each file straight away. Only small JSON extracts are kept, in `data/runs/`.
4. It computes the freezing level and the wet-bulb 0 °C height (≈ snow level) from the vertical temperature/dewpoint profile. It also computes model ground elevation from surface pressure and, optionally, wind interpolated to a chosen elevation.
5. Every chart that uses an updated model is re-rendered (`data/charts/<id>/latest.png`) and posted to its webhook.

### Built-in models

| key | model | runs (UTC) | range | notes |
|---|---|---|---|---|
| `hrdps` | HRDPS 2.5 km continental | 00/06/12/18 | 48 h | upper air every 3 h by default (`profile_step_hours`), precip hourly |
| `hrdps_west` | HRDPS 1 km West (experimental, `dd.alpha`) | 00/12 | 48 h | ~6 h publication delay, 24 h history |
| `rdps` | RDPS 10 km | 00/06/12/18 | 84 h | |
| `gdps` | GDPS 15 km | 00/12 | 240 h | upper air every 3 h |

All four use the same pipeline. A new Datamart model only needs URL/filename templates and variable names in `config.yaml` under `models:`, and you can override any built-in field there too. See `skiforecast/models.py` for the format.

## Quick start

```bash
cp .env.example .env                              # put your Discord webhook URLs here
cp config/config.example.yaml config/config.yaml  # edit points + charts
docker compose build
docker compose run --rm skiforecast check-config  # validates config, estimates download volume
docker compose run --rm skiforecast test-discord alerts
docker compose up -d
docker compose logs -f
```

The first start processes the latest run of every model, which takes several minutes, and then posts all charts (`post_on_startup`).

### Commands

`docker compose run --rm skiforecast <command>`

| command | what it does |
|---|---|
| `run` | the service (default) |
| `once` | one poll cycle, then exit |
| `check-config` | validate config and print a summary + GB/day estimate |
| `render <chart> [--post]` | re-render a chart from stored data (handy after style tweaks) |
| `test-discord <chart-id or alerts>` | send a test message |
| `healthcheck` | used by the Docker healthcheck (heartbeat age) |

## Configuration

See `config/config.example.yaml`. The main ideas:

**Points** are named lat/lon locations. A chart source references a point and can shift it by whole grid cells with `offset: [east, north]`. For example, `[-1, -1]` is one cell left and one down, which is the SpotWX "move a tile" trick. The chart footer prints the grid cell actually used and the model's ground elevation there, so you can check your choice.

**Charts** have two layers, each an ordered list of sources:

```yaml
freezing_level:
  - { model: hrdps, point: whistler_peak, until_hour: 48 }    # first 48 h of its run
  - { model: gdps,  point: whistler_peak, offset: [-1, -1] }  # then GDPS to the end
precipitation:            # optional; defaults to the freezing_level sources
  - { model: gdps, point: whistler_peak, offset: [-1, -1] }
```

- Sources take over from each other in order. Each one covers time from where the previous one ended up to its own `until_hour` (counted from its own run start) or the end of its data.
- A source with no data yet is skipped, so a list doubles as a fallback chain. For example, `hrdps_west` then `hrdps` uses HRDPS whenever the experimental 1 km run is late.
- Freezing level and precipitation can use completely different models and points.

Other per-chart options:

- `hours` and `history_hours`: chart length, and how much past time to keep.
- `freezing_level_method`: `highest` (the top of any warm layer, above which everything is frozen) or `lowest`.
- `show_wetbulb_zero`: draws the dashed snow-level estimate.
- Precipitation: `precip_bar_hours`, `precip_total_hours` and `precip_total_start_hour_local` (boxed totals).
- `wind`: `enabled`, `elevation_m`, `every_hours`, and which layer's sources to use.
- `reference_lines`: horizontal elevation markers.
- `style`: size, axis limits, colours, footer, model-transition marker, now line and tick hour.
- Discord: `webhook` (per chart, otherwise `discord.default_webhook`), `message` (text sent with the image, e.g. `@here`) and `min_post_interval_minutes`.

Titles can use `{fl_models}` and `{precip_models}`, e.g. `"Whistler — 10-day ({fl_models})"` becomes `HRDPS→GDPS`.

**Alerts** (`discord.alerts_webhook`) cover start-up, processing or rendering errors and a model going stale (`stale_after_hours` per model). A recovery notice follows each one, and repeats are throttled by `alert_repeat_hours`.

### Bandwidth

The Datamart serves whole national or global grids, so a point forecast still has to download full files. `check-config` prints an estimate. The example config uses about 25 GB/day, and nothing is kept on disk. To reduce it:

- Raise `profile_step_hours`.
- Lower `profile_top_hpa`. The default of 600 hPa is about 4.2 km.
- Turn off `show_wetbulb_zero` (saves the dewpoint files).
- Turn off wind.
- Use `until_hour` to stop a model earlier.

## Notes on accuracy

- The freezing level is a height above sea level taken from the model atmosphere, so it hardly depends on which grid cell you pick. Precipitation does depend on the cell, through orographic effects in the model terrain.
- The wet-bulb 0 °C height is a good first guess at the snow level during precipitation. Snow often reaches 100–300 m lower in heavy precipitation.
- HRDPS-West is an experimental product with no guaranteed availability.
