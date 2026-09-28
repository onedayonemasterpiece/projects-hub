from __future__ import annotations

import os
import uvicorn

from projects_hub.app import create_app


if __name__ == "__main__":
    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=int(os.environ.get("PROJECTS_HUB_PORT", "8196")),
        log_config=None,
        access_log=False,
    )
