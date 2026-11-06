"""Minimal Discord webhook client (chart posts and service alerts)."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

COLORS = {"chart": 0x3A6FB0, "error": 0xC0392B, "warning": 0xE67E22, "ok": 0x27AE60, "info": 0x7F8C8D}


class Discord:
    def __init__(self, username: str = "Ski Forecast", avatar_url: Optional[str] = None,
                 enabled: bool = True, timeout: float = 30):
        self.username = username
        self.avatar_url = avatar_url
        self.enabled = enabled
        self.timeout = timeout

    def _post(self, url: str, payload: dict, file: Optional[Path] = None) -> bool:
        if not self.enabled or not url:
            log.info("discord disabled/no webhook; would post: %s", payload.get("embeds", [{}])[0].get("title"))
            return False
        payload = {"username": self.username, **payload}
        if self.avatar_url:
            payload["avatar_url"] = self.avatar_url
        for attempt in range(5):
            try:
                if file:
                    with open(file, "rb") as fh:
                        r = requests.post(url, data={"payload_json": json.dumps(payload)},
                                          files={"files[0]": (file.name, fh, "image/png")}, timeout=self.timeout)
                else:
                    r = requests.post(url, json=payload, timeout=self.timeout)
            except requests.RequestException as e:
                log.warning("discord post failed (%s), retrying", e)
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                wait = float(r.json().get("retry_after", 2))
                time.sleep(min(wait, 60))
                continue
            if r.status_code >= 400:
                log.error("discord webhook returned %s: %s", r.status_code, r.text[:300])
                if r.status_code < 500:
                    return False
                time.sleep(2 ** attempt)
                continue
            return True
        return False

    def post_chart(self, url: str, image: Path, title: str, description: str, content: str = "") -> bool:
        embed = {"title": title[:256], "description": description[:4000], "color": COLORS["chart"],
                 "image": {"url": f"attachment://{image.name}"},
                 "timestamp": datetime.now(timezone.utc).isoformat()}
        return self._post(url, {"content": content[:2000], "embeds": [embed]}, image)

    def alert(self, url: Optional[str], title: str, description: str, level: str = "error") -> bool:
        embed = {"title": title[:256], "description": description[:4000], "color": COLORS.get(level, 0),
                 "timestamp": datetime.now(timezone.utc).isoformat()}
        return self._post(url or "", {"embeds": [embed]})
