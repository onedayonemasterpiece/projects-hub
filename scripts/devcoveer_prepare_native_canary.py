#!/usr/bin/env python3
"""Build an exact candidate for isolated provider acceptance, without activation.

Uses the product installer's existing archive/build and private-package path.
The result is a separate build directory; no service, current release symlink,
production data, authority configuration or credential is changed.
Run the actual canary separately using this build's Python and source.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--sdk-sha", required=True)
    parser.add_argument("--build-root", required=True, type=Path)
    args = parser.parse_args()
    for value in (args.sha, args.sdk_sha):
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            parser.error("Both source revisions must be exact commit SHAs")
    project = Path(__file__).resolve().parents[1]
    root = args.build_root.resolve()
    if not root.is_relative_to(project) or root == project:
        parser.error("Use a separate build directory inside this checkout")
    if root.exists() and any(root.iterdir()):
        parser.error("Build directory must be new or empty; preserve earlier receipts")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("native_canary_installer", project / "deploy/devcoveer_install.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("CANARY_INSTALLER_UNAVAILABLE")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    installer.resolve_sha(args.sha)
    installer.RELEASES_ROOT = root
    installer.AI_RESOURCE_CONTROL_SHA = args.sdk_sha
    installer.AI_RESOURCE_CONTROL_URL = (
        "git+https://github.com/onedayonemasterpiece/ai-resource-control.git@" + args.sdk_sha
    )
    # Deliberately invoke only the build step. Production deploy() still requires
    # fresh main ancestry and owns its unchanged activation/rollback transaction.
    release = installer._build_release(args.sha)
    receipt = {
        "status": "prepared_for_isolated_acceptance",
        "projects_hub_sha": args.sha,
        "ai_resource_control_sha": args.sdk_sha,
        "release": str(release),
        "python": str(release / "venv/bin/python"),
        "production_activation": False,
        "provider_calls_attempted": False,
    }
    (root / "canary-build.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
