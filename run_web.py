"""Run only the web control panel."""
from __future__ import annotations

import uvicorn

from app.config import get_config


def main() -> None:
    cfg = get_config()
    uvicorn.run(
        "app.web.main:app",
        host=cfg.web.host,
        port=cfg.web.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
