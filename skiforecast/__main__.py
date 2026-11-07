"""Command line entry point:  python -m skiforecast <command>"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from .config import ConfigError, load_config

DEFAULT_CONFIG = os.environ.get("SFG_CONFIG", "/config/config.yaml")

# rough average file sizes (MB) for download estimates
_AVG_MB = {"hrdps": 2.0, "hrdps_west": 0.6, "rdps": 0.8, "gdps": 1.6}


def _setup_logging():
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_check(cfg) -> int:
    from .datamart import MAX_RADIUS_CELLS, compute_needs, profile_hours, precip_hours
    needs = compute_needs(cfg)
    print("Configuration OK\n")
    print(f"Timezone: {cfg.general.timezone}   poll every {cfg.general.poll_interval_minutes} min   "
          f"data dir {cfg.general.data_dir}")
    print(f"Discord: {'enabled' if cfg.discord.enabled else 'DISABLED'}   alerts webhook: "
          f"{'set' if cfg.discord.alerts_webhook else 'not set'}\n")
    print("Charts:")
    from .config import SnowChartConfig

    def chain(sources):
        return " → ".join(f"{s.model}@{s.point}{'' if tuple(s.offset)==(0,0) else tuple(s.offset)}"
                          f"{'' if s.until_hour is None else f'(≤{s.until_hour}h)'}" for s in sources)

    for c in cfg.charts:
        state = "" if c.enabled else " [disabled]"
        if isinstance(c, SnowChartConfig):
            b = c.blend
            elevs = "/".join(f"{e.elevation:g}" for e in c.elevations)
            if b.neighbourhood_km > 0:
                # cell count depends on the grid spacing, known only once a file is read
                w = 2 * MAX_RADIUS_CELLS + 1
                hood = f"up to {w}×{w} cells ({b.neighbourhood_km:g} km)"
            else:
                hood = "centre cell only"
            wind = " + wind" if b.use_wind else ""
            print(f"  {c.id}{state}: {c.hours}h blended snow, elevations {elevs} m\n"
                  f"     sources: {chain(c.sources)}\n"
                  f"     blend: {b.lagged_runs} runs × {hood} × phase/ratio perturbations{wind}")
        else:
            print(f"  {c.id}{state}: {c.hours}h\n     freezing level: {chain(c.freezing_level)}\n"
                  f"     precipitation:  {chain(c.precip_sources)}")
    print("\nModel downloads (estimate):")
    total_day = 0.0
    n_levels = lambda m: len([l for l in m.levels if l >= cfg.general.profile_top_hpa])
    for key, n in sorted(needs.items()):
        m = cfg.resolved_models[key]
        files = 0
        if n.need_profile:
            files += len(profile_hours(m, n.max_hour)) * (1 + len(n.profile_roles) * n_levels(m))
        if n.need_precip:
            files += len(precip_hours(m, n.max_hour))
        mb = files * _AVG_MB.get(key, 1.0)
        day = mb * len(m.runs) / 1000
        total_day += day
        print(f"  {key:12s} {len(n.points)} point(s), 0-{n.max_hour}h, {files} files/run ≈ {mb/1000:.1f} GB/run, "
              f"{len(m.runs)} runs/day ≈ {day:.1f} GB/day")
    print(f"  total ≈ {total_day:.1f} GB/day (files are deleted right after extraction)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="skiforecast", description="Ski forecast meteogram generator")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="run the polling service (default)")
    sub.add_parser("once", help="run a single poll cycle and exit")
    sub.add_parser("check-config", help="validate the configuration and print a summary")
    r = sub.add_parser("render", help="render a chart from stored data")
    r.add_argument("chart")
    r.add_argument("--post", action="store_true", help="also post it to Discord")
    t = sub.add_parser("test-discord", help="send a test message to a chart webhook or the alerts webhook")
    t.add_argument("target", help="chart id or 'alerts'")
    h = sub.add_parser("healthcheck", help="exit 0 if the service loop is alive")
    h.add_argument("--max-age-minutes", type=float, default=None)
    args = ap.parse_args(argv)
    cmd = args.cmd or "run"
    _setup_logging()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"CONFIG ERROR ({args.config}):\n{e}", file=sys.stderr)
        return 2

    if cmd == "check-config":
        return cmd_check(cfg)

    if cmd == "healthcheck":
        from pathlib import Path
        hb = Path(cfg.general.data_dir) / "heartbeat"
        limit = args.max_age_minutes or max(30.0, cfg.general.poll_interval_minutes * 3)
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(hb.read_text().strip())
        except (FileNotFoundError, ValueError):
            print("no heartbeat yet")
            return 1
        ok = age < timedelta(minutes=limit)
        print(f"heartbeat age {age.total_seconds()/60:.1f} min ({'ok' if ok else 'STALE'})")
        return 0 if ok else 1

    from .service import Service
    svc = Service(cfg)
    if cmd == "run":
        svc.run_forever()
    elif cmd == "once":
        svc.poll_once()
    elif cmd == "render":
        chart = next((c for c in cfg.charts if c.id == args.chart), None)
        if chart is None:
            print(f"unknown chart {args.chart!r}", file=sys.stderr)
            return 2
        path, data, _ = svc.render(chart)
        print(path)
        if args.post:
            svc.discord.post_chart(cfg.webhook_for(chart), path, data.title, "Manual render", chart.message)
    elif cmd == "test-discord":
        if args.target == "alerts":
            ok = svc.discord.alert(cfg.discord.alerts_webhook, "Test alert", "Alerts webhook works.", "info")
        else:
            chart = next((c for c in cfg.charts if c.id == args.target), None)
            if chart is None:
                print(f"unknown chart {args.target!r}", file=sys.stderr)
                return 2
            ok = svc.discord.alert(cfg.webhook_for(chart), f"Test: {chart.title}", "Chart webhook works.", "info")
        print("sent" if ok else "FAILED (see log)")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
