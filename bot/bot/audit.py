"""Bot-side audit trail.

The backend records the *service account* as actor for alert acknowledge / snooze / resolve
(it ignores any `acknowledged_by`-style body fields), so the human behind a Telegram action
is only recorded here: one JSON line per action in `BOT_AUDIT_FILE` (if set) and in the
`bot.audit` logger. The only backend field that does carry the human is
`ValveCommand.issued_by` (set for valve commands).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("bot.audit")


class AuditLog:
    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path) if path else None

    def record(self, event: str, actor: str, **fields: Any) -> None:
        entry = {"ts": round(time.time(), 3), "event": event, "actor": actor, **fields}
        line = json.dumps(entry, default=str, sort_keys=True)
        log.info("audit %s", line)
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            log.error("could not write audit file %s", self._path, exc_info=True)
