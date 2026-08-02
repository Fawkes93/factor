"""Start Factor (uvicorn)."""

from __future__ import annotations

import logging
import os

import uvicorn

from factor import config
from factor.api.app import create_app


class _QuietHealthAccessFilter(logging.Filter):
    """Drop access-log lines for health probes (Render hits them every few seconds)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        # uvicorn: '10.x.x.x:port - "GET /api/health/ HTTP/1.1" 200 OK'
        if "/api/health" in msg:
            return False
        if '"HEAD / HTTP/' in msg or '"HEAD / ' in msg:
            return False
        return True


def main() -> None:
    app = create_app()
    logging.getLogger("uvicorn.access").addFilter(_QuietHealthAccessFilter())
    # Prefer env PORT (Render) already baked into config.PORT
    # workers=1: single process on 512MB — never double RAM with multi-worker.
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level=os.getenv("LOG_LEVEL", "info"),
        workers=1,
        limit_concurrency=50,
    )


if __name__ == "__main__":
    main()
