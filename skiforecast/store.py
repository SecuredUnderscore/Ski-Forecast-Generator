"""On-disk state: processed runs (small JSON point extracts) and service state."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _atomic_write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str))
    os.replace(tmp, path)


class Store:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)
        self.runs = self.root / "runs"
        self.charts = self.root / "charts"
        self.state_path = self.root / "state.json"
        self.heartbeat_path = self.root / "heartbeat"
        for d in (self.runs, self.charts):
            d.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    # ------------------------------------------------------------------ runs
    def save_run(self, model: str, data: dict, keep: int):
        _atomic_write(self.runs / model / f"{data['run']}.json", data)
        files = sorted((self.runs / model).glob("*.json"))
        for old in files[:-keep]:
            old.unlink(missing_ok=True)

    def run_ids(self, model: str) -> list[str]:
        return sorted(p.stem for p in (self.runs / model).glob("*.json"))

    def latest_run(self, model: str) -> Optional[dict]:
        ids = self.run_ids(model)
        if not ids:
            return None
        return json.loads((self.runs / model / f"{ids[-1]}.json").read_text())

    def recent_runs(self, model: str, n: int) -> list[dict]:
        """Up to ``n`` newest stored runs of ``model``, newest first."""
        out = []
        for rid in sorted(self.run_ids(model), reverse=True)[:max(0, n)]:
            try:
                out.append(json.loads((self.runs / model / f"{rid}.json").read_text()))
            except (FileNotFoundError, json.JSONDecodeError):
                continue   # pruned or half-written meanwhile
        return out

    # ------------------------------------------------------------------ state
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"models": {}, "charts": {}, "alerts": {}}

    def save_state(self):
        _atomic_write(self.state_path, self.state)

    def model_state(self, model: str) -> dict:
        return self.state.setdefault("models", {}).setdefault(model, {})

    def chart_state(self, chart: str) -> dict:
        return self.state.setdefault("charts", {}).setdefault(chart, {})

    def beat(self):
        self.heartbeat_path.write_text(datetime.now(timezone.utc).isoformat())

    def chart_path(self, chart_id: str, stamp: str, keep: int) -> Path:
        d = self.charts / chart_id
        d.mkdir(parents=True, exist_ok=True)
        old = sorted(p for p in d.glob("*.png") if p.name != "latest.png")
        for p in old[: max(0, len(old) - keep + 1)]:
            p.unlink(missing_ok=True)
        return d / f"{stamp}.png"
