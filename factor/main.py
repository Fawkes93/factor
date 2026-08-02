"""Start Factor (uvicorn)."""

from __future__ import annotations

import logging
import os
from copy import deepcopy
from typing import Any, Dict

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from factor import config
from factor.api.app import create_app


class QuietHealthAccessFilter(logging.Filter):
    """
    Drop access-log lines for Render health / port probes.

    uvicorn rebuilds loggers on run(); attach via log_config filters so this sticks.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Preferred: structured access args (client, method, path, http_version, status)
        try:
            args = record.args
            if isinstance(args, (list, tuple)) and len(args) >= 3:
                path = str(args[2])
                method = str(args[1]) if len(args) > 1 else ""
                if path.startswith("/api/health"):
                    return False
                if method == "HEAD" and path in ("/", ""):
                    return False
        except Exception:
            pass
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "/api/health" in msg:
            return False
        if "HEAD / HTTP" in msg or '"HEAD / ' in msg:
            return False
        return True


def _access_log_config() -> Dict[str, Any]:
    cfg = deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})
    cfg["filters"]["quiet_health"] = {
        "()": "factor.main.QuietHealthAccessFilter",
    }
    access_handler = cfg.get("handlers", {}).get("access")
    if isinstance(access_handler, dict):
        access_handler["filters"] = ["quiet_health"]
    return cfg


def main() -> None:
    app = create_app()
    # Prefer env PORT (Render) already baked into config.PORT
    # workers=1: single process on 512MB — never double RAM with multi-worker.
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level=os.getenv("LOG_LEVEL", "info"),
        workers=1,
        limit_concurrency=50,
        log_config=_access_log_config(),
    )


if __name__ == "__main__":
    main()
