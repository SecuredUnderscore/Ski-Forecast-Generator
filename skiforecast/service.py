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
from .config import AppConfig, ChartBase, SnowChartConfig
from .datamart import Datamart, RunIncomplete, compute_needs, parse_run_id, run_id
from .discord import Discord
from .store import Store

log = logging.getLogger(__name__)
UTC = timezone.utc


def due_slot(times: list[str], now: datetime, tz: ZoneInfo) -> datetime:
    """Most recent scheduled local wall-clock time (``"HH:MM"`` in ``tz``, today or
    yesterday) that is <= ``now``, as an aware UTC datetime.  DST is handled by
    ZoneInfo (a nonexistent spring-forward time maps to the shifted instant)."""
    if not times:
        raise ValueError("no scheduled times")
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    today = now.astimezone(tz).date()
    best: Optional[datetime] = None
    for day in (today, today - timedelta(days=1)):
        for t in times:
            hh, mm = (int(x) for x in t.split(":"))
            cand = datetime(day.year, day.month, day.day, hh, mm).replace(tzinfo=tz).astimezone(UTC)
            if cand <= now and (best is None or cand > best):
                best = cand
    assert best is not None   # yesterday's slots are always in the past
    return best


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
    def chart_models(self, chart: ChartBase) -> list[str]:
        return chart.models()

    def render(self, chart: ChartBase, runs: Optional[dict] = None):
        """Render ``chart`` and copy it to ``latest.png``.

        Meteogram: ``runs`` is ``{model: run}`` (default: the latest stored runs).
        Blended snow: ``runs`` is ``{model: [run, ...]}`` newest first (default: the
        ``blend.lagged_runs`` most recent stored runs of each model).
        Returns ``(path, data, latest_runs)`` with ``latest_runs = {model: newest run}``.
        """
        if isinstance(chart, SnowChartConfig):
            from .blend import build_snow_chart_data
            from .snowchart import render_snow_chart
            if runs is None:
                runs = {m: self.store.recent_runs(m, chart.blend.lagged_runs) for m in self.chart_models(chart)}
            runs = {k: list(v) for k, v in runs.items() if v}
            data = build_snow_chart_data(self.cfg, chart, runs)
            renderer = render_snow_chart
            latest = {k: v[0] for k, v in runs.items()}
        else:
            runs = runs or {m: self.store.latest_run(m) for m in self.chart_models(chart)}
            runs = {k: v for k, v in runs.items() if v}
            data = build_chart_data(self.cfg, chart, runs)
            renderer = render_chart
            latest = runs
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.store.chart_path(chart.id, stamp, self.cfg.general.keep_charts)
        renderer(data, path)
        shutil.copyfile(path, path.parent / "latest.png")
        return path, data, latest

    def maybe_post(self, chart: ChartBase, updated: set[str], now: datetime):
        """Render and post ``chart`` when its posting policy says so.

        State per chart (state.json -> charts.<id>): ``signature`` (the
        ``model:run`` list of the data last posted / acknowledged), ``posted_at``
        and, in schedule mode, ``scheduled_slot`` (UTC ISO time of the last
        handled schedule slot).

        Update mode (``post.times`` empty):
          * post when the signature (latest run of every chart model) changed AND
            either ``post.models`` is empty or one of the changed models is listed
            in ``post.models``;
          * if only non-trigger models changed, nothing happens and the stored
            signature is kept, so the eventual post's "Updated:" line lists every
            model that changed since the previous post.
        Schedule mode (``post.times`` set: local wall-clock times in
        ``general.timezone``):
          * new model runs never trigger a post by themselves;
          * post (with the latest data) when the most recent due slot
            (``due_slot``) differs from ``scheduled_slot``; a service that missed
            several slots posts once, for the latest one;
          * with no data at all the slot is marked without posting.
        Both modes:
          * first cycle with ``general.post_on_startup: false`` and no stored state
            for the chart: record the signature (and the current slot) silently.
            With ``post_on_startup: true`` the first cycle posts normally (schedule
            mode: immediately, unless that slot was already handled before a
            restart; update mode: unless the signature is unchanged);
          * ``min_post_interval_minutes`` throttles posts; a throttled scheduled
            post leaves the slot unmarked so a later poll posts it.
        """
        models = self.chart_models(chart)
        latest = {m: self.store.latest_run(m) for m in models}
        signature = ",".join(f"{m}:{latest[m]['run']}" for m in models if latest.get(m))
        cs = self.store.chart_state(chart.id)
        old = dict(p.split(":", 1) for p in cs.get("signature", "").split(",") if ":" in p)
        changed = [m for m in models if latest.get(m) and old.get(m) != latest[m]["run"]]
        tz = ZoneInfo(self.cfg.general.timezone)
        startup_silent = self.first_cycle and not self.cfg.general.post_on_startup

        slot = None
        if chart.post.scheduled:
            slot_dt = due_slot(chart.post.times, now, tz)
            slot = slot_dt.isoformat()
            if cs.get("scheduled_slot") == slot:
                return
            if startup_silent and "scheduled_slot" not in cs and "signature" not in cs:
                cs["scheduled_slot"] = slot
                if signature:
                    cs["signature"] = signature
                return
            if not signature:
                cs["scheduled_slot"] = slot
                return
            if self._throttled(chart, cs, now):
                return
            header = f"**Scheduled post** {slot_dt.astimezone(tz):%H:%M}"
        else:
            if not signature or cs.get("signature") == signature:
                return
            if startup_silent and "signature" not in cs:
                cs["signature"] = signature
                return
            if chart.post.models and not any(m in chart.post.models for m in changed):
                return
            if self._throttled(chart, cs, now):
                return
            header = ""

        if isinstance(chart, SnowChartConfig):
            path, data, runs = self.render(chart)
        else:
            path, data, runs = self.render(chart, latest)
        if not header:
            new_bits = [f"{self.cfg.resolved_models[m].label} {runs[m]['run'][9:12]}"
                        for m in models if runs.get(m) and old.get(m) != runs[m]["run"]]
            header = f"**Updated:** {', '.join(new_bits)}" if new_bits else ""
        lines = [header]
        for s in data.sources:
            lines.append(f"{s.role}: {s.model_label} {format_zulu_local(s.run, tz, '%d %b %HZ')} ({s.hours})")
        recipe = getattr(data, "recipe", "")
        if recipe:
            lines.append(recipe)
        ok = self.discord.post_chart(self.cfg.webhook_for(chart), path, data.title,
                                     "\n".join(l for l in lines if l), chart.message)
        cs.update(signature=signature, rendered=str(path), posted_at=now.isoformat(), posted=ok)
        if slot is not None:
            cs["scheduled_slot"] = slot
        self.store.save_state()
        log.info("chart %s rendered (%s) posted=%s", chart.id, path.name, ok)

    @staticmethod
    def _throttled(chart: ChartBase, cs: dict, now: datetime) -> bool:
        if cs.get("posted_at") and chart.min_post_interval_minutes:
            return now - datetime.fromisoformat(cs["posted_at"]) < timedelta(minutes=chart.min_post_interval_minutes)
        return False

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
