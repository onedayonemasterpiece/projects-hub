"""Non-secret identities of the code actually loaded in this process.

A release manifest describes an available update, never the running application.
Client build information is display-only and grants no authority.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import re
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def loaded_live_sdk() -> dict[str, Any]:
    try:
        module = importlib.import_module("live_interaction")
    except ImportError:
        return {"version": None, "source_sha": None}
    version = getattr(module, "__version__", None)
    source_sha = None
    try:
        distribution = importlib.metadata.distribution("live-interaction")
        origin = json.loads(distribution.read_text("direct_url.json") or "{}")
        candidate = origin.get("vcs_info", {}).get("commit_id")
        if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{40}", candidate):
            source_sha = candidate
    except (importlib.metadata.PackageNotFoundError, ValueError, OSError, TypeError, AttributeError):
        pass
    # Never return download URLs, environment values or local filesystem paths.
    return {"version": version if isinstance(version, str) else None, "source_sha": source_sha}
