"""Start Factor (uvicorn)."""

from __future__ import annotations

import os

import uvicorn

from factor import config
from factor.api.app import create_app


def main() -> None:
    app = create_app()
    # Prefer env PORT (Render) already baked into config.PORT
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level=os.getenv("LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
