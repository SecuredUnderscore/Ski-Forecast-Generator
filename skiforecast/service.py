"""The polling service: detect new runs, process them, render and post charts."""
from __future__ import annotations

import logging
import shutil
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .assemble import build_chart_data
from .chart import format_zulu_local, render_chart
from .config import AppConfig, ChartConfig
from .datamart import Datamart, RunIncomplete, compute_needs, parse_run_id, run_id
from .discord import Discord
from .store import Store

log = logging.getLogger(__name__)
UTC = timezone.utc


class Service:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.store = Store(cfg.general.data_dir)
        self.dm = Datamart(cfg)
        d = cfg.discord
        self.discord = Discord(d.username, d.avatar_url, d.enabled, cfg.general.http_timeout_seconds)
        self.needs = compute_needs(cfg)
        self.first_cycle = True

    # ------------------------------------------------------------------ alerts
    def alert(self, key: str, title: str, text: str, level: str = "error", force: bool = False):
        log.log(logging.ERROR if level == "error" else logging.WARNING, "%s: %s", title, text)
        alerts = self.store.state.setdefault("alerts", {})
        last = alerts.get(key)
        now = datetime.now(UTC)
        if not force and last and now - datetime.fromisoformat(last) < timedelta(hours=self.cfg.discord.alert_repeat_hours):
            return
        if self.discord.alert(self.cfg.discord.alerts_webhook, title, text, level):
            alerts[key] = now.isoformat()

    def clear_alert(self, key: str, title: str, text: str):
        alerts = self.store.state.setdefault("alerts", {})
        if alerts.pop(key, None):
            self.discord.alert(self.cfg.discord.alerts_webhook, title, text, "ok")

    # ------------------------------------------------------------------ models
    def update_model(self, key: str, now: datetime) -> bool:
        m = self.cfg.resolved_models[key]
        needs = self.needs[key]
        latest = self.store.latest_run(key)
        latest_init = parse_run_id(latest["run"]) if latest else None
        latest_ok = bool(latest) and latest.get("signature") == needs.signature()
        ms = self.store.model_state(key)
        ms["last_check"] = now.isoformat()

        for init in self.dm.candidate_runs(m, now):
            if latest_init and (init < latest_init or (init == latest_init and latest_ok)):
                break
            levels = self.dm.check_complete(m, needs, init)
            if levels is None:
                log.debug("%s %s not complete yet", key, run_id(init))
                continue
            log.info("%s: new run %s, processing", key, run_id(init))
            try:
                data = self.dm.process_run(key, needs, init, levels,
                                           progress=lambda d, t: (self.store.beat(),
                                                                  log.info("%s %s: %d/%d files", key, run_id(init), d, t)))
            except RunIncomplete as e:
                log.info("%s %s: %s; will retry", key, run_id(init), e)
                continue
            self.store.save_run(key, data, self.cfg.general.keep_runs)
            ms.update(last_run=data["run"], processed_at=data["processed_at"], last_bytes=data["bytes"])
            self.store.save_state()
            return True
        return False

    def check_stale(self, key: str, now: datetime):
        m = self.cfg.resolved_models[key]
        latest = self.store.latest_run(key)
        akey = f"stale:{key}"
        if latest is None:
            return
        age = (now - parse_run_id(latest["run"])).total_seconds() / 3600
        if age > m.stale_after_hours:
            self.alert(akey, f"{m.label}: no new run for {age:.0f} h",
                       f"Latest processed run is {latest['run']} (threshold {m.stale_after_hours} h). "
                       f"The Datamart may be delayed or the file layout may have changed.", "warning")
        else:
            self.clear_alert(akey, f"{m.label}: back to normal", f"Processed run {latest['run']}.")

    # ------------------------------------------------------------------ charts
    def chart_models(self, chart: ChartConfig) -> list[str]:
        return list(dict.fromkeys(s.model for s in chart.freezing_level + chart.precip_sources))

    def render(self, chart: ChartConfig, runs: Optional[dict] = None):
        runs = runs or {m: self.store.latest_run(m) for m in self.chart_models(chart)}
        runs = {k: v for k, v in runs.items() if v}
        data = build_chart_data(self.cfg, chart, runs)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.store.chart_path(chart.id, stamp, self.cfg.general.keep_charts)
        render_chart(data, path)
        shutil.copyfile(path, path.parent / "latest.png")
        return path, data, runs

    def maybe_post(self, chart: ChartConfig, updated: set[str], now: datetime):
        models = self.chart_models(chart)
        runs = {m: self.store.latest_run(m) for m in models}
        signature = ",".join(f"{m}:{runs[m]['run']}" for m in models if runs.get(m))
        if not signature:
            return
        cs = self.store.chart_state(chart.id)
        if cs.get("signature") == signature:
            return
        if self.first_cycle and not self.cfg.general.post_on_startup and "signature" not in cs:
            cs["signature"] = signature
            return
        if cs.get("posted_at") and chart.min_post_interval_minutes:
            if now - datetime.fromisoformat(cs["posted_at"]) < timedelta(minutes=chart.min_post_interval_minutes):
                return
        path, data, runs = self.render(chart, runs)
        old = dict(p.split(":") for p in cs.get("signature", "").split(",") if ":" in p)
        new_bits = [f"{self.cfg.resolved_models[m].label} {runs[m]['run'][9:12]}"
                    for m in models if runs.get(m) and old.get(m) != runs[m]["run"]]
        lines = [f"**Updated:** {', '.join(new_bits)}" if new_bits else ""]
        tz = ZoneInfo(self.cfg.general.timezone)
        for s in data.sources:
            lines.append(f"{s.role}: {s.model_label} {format_zulu_local(s.run, tz, '%d %b %HZ')} ({s.hours})")
        ok = self.discord.post_chart(self.cfg.webhook_for(chart), path, data.title,
                                     "\n".join(l for l in lines if l), chart.message)
        cs.update(signature=signature, rendered=str(path), posted_at=now.isoformat(), posted=ok)
        self.store.save_state()
        log.info("chart %s rendered (%s) posted=%s", chart.id, path.name, ok)

    # ------------------------------------------------------------------ loop
    def poll_once(self):
        now = datetime.now(UTC)
        updated: set[str] = set()
        for key in sorted(self.needs):
            try:
                if self.update_model(key, now):
                    updated.add(key)
                self.clear_alert(f"error:{key}", f"{key}: recovered", "Processing works again.")
            except Exception as e:  # keep the service alive
                self.alert(f"error:{key}", f"Error processing {key}",
                           f"```{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}```")
            self.check_stale(key, now)
            self.store.beat()
        for chart in self.cfg.enabled_charts():
            try:
                self.maybe_post(chart, updated, now)
                self.clear_alert(f"chart:{chart.id}", f"Chart {chart.id}: recovered", "Rendering works again.")
            except Exception as e:
                self.alert(f"chart:{chart.id}", f"Error rendering chart {chart.id}",
                           f"```{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}```")
        self.first_cycle = False
        log.log(logging.INFO if updated else logging.DEBUG, "poll done; new runs: %s",
                ", ".join(sorted(updated)) or "none")
        self.store.beat()
        self.store.save_state()

    def run_forever(self):
        if self.cfg.discord.startup_message:
            charts = ", ".join(c.id for c in self.cfg.enabled_charts())
            self.discord.alert(self.cfg.discord.alerts_webhook, "Ski forecast service started",
                               f"Charts: {charts}\nModels: {', '.join(sorted(self.needs))}", "info")
        interval = self.cfg.general.poll_interval_minutes * 60
        while True:
            t0 = time.time()
            try:
                self.poll_once()
            except Exception:
                log.exception("poll cycle failed")
            time.sleep(max(10, interval - (time.time() - t0)))
