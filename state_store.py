"""Persistent sent-signal registry used for duplicate prevention."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

log = logging.getLogger("state")


class SentSignalStore:
    """JSON file: {"version": 1, "sent": {signal_id: {"sent_at": ISO-8601 UTC}}}."""

    def __init__(self, path: str, retention_days: int = 14) -> None:
        self.path = path
        self.retention = timedelta(days=retention_days)
        self._sent: Dict[str, Dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            sent = data.get("sent", {})
            if isinstance(sent, dict):
                self._sent = {str(k): dict(v) for k, v in sent.items() if isinstance(v, dict)}
        except FileNotFoundError:
            log.info("No state file yet (%s) - starting empty", self.path)
        except (ValueError, OSError, AttributeError, TypeError) as exc:
            # A corrupt state file must not crash the scan. The freshness window in the
            # scanner still prevents old signals from being re-sent as new.
            log.warning("State file unreadable (%s) - starting empty", type(exc).__name__)

    def __contains__(self, signal_id: str) -> bool:
        return signal_id in self._sent

    def __len__(self) -> int:
        return len(self._sent)

    def add(self, signal_id: str, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        self._sent[signal_id] = {"sent_at": now.isoformat()}

    def prune(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        cutoff = now - self.retention
        drop = []
        for sid, meta in self._sent.items():
            try:
                ts = datetime.fromisoformat(meta.get("sent_at", ""))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    drop.append(sid)
            except ValueError:
                drop.append(sid)
        for sid in drop:
            del self._sent[sid]
        return len(drop)

    def save(self) -> None:
        """Atomic write (temp file + rename) so an interrupted run can't corrupt state."""
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sent_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "sent": self._sent}, fh, indent=1, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
