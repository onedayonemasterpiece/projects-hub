#!/usr/bin/env python3
"""Pin the disposable CI guest after emulator-runner installs the SDK image."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.request import urlopen
from zipfile import ZipFile


def main() -> None:
    sdk = Path(os.environ["ANDROID_HOME"]).resolve()
    destination = sdk / "system-images/android-31/google_apis/x86_64"
    evidence = Path("android/smoke-evidence")
    evidence.mkdir(parents=True, exist_ok=True)
    url = "https://dl.google.com/android/repository/sys-img/google_apis/x86_64-31_r14.zip"
    # The action has already created its AVD. Its emulator launch uses -wipe-data
    # so the guest userdata matches this exact system image. No SDK install runs
    # after this hook; normal APK compilation still uses Android SDK 35.
    with tempfile.TemporaryDirectory(prefix="ph-ci-api31-", dir=os.environ["RUNNER_TEMP"]) as temporary:
        root = Path(temporary)
        archive = root / "image.zip"
        digest = hashlib.sha256()
        with urlopen(url, timeout=60) as response, archive.open("wb") as output:
            while block := response.read(1024 * 1024):
                output.write(block)
                digest.update(block)
        unpacked = root / "unpacked"
        unpacked.mkdir()
        with ZipFile(archive) as source:
            for entry in source.infolist():
                target = (unpacked / entry.filename).resolve()
                if not target.is_relative_to(unpacked):
                    raise ValueError("System image archive path escapes its extraction root")
            source.extractall(unpacked)
        candidates = list(unpacked.rglob("source.properties"))
        if len(candidates) != 1:
            raise ValueError("Expected exactly one system image package")
        package = candidates[0].parent
        properties = dict(
            line.split("=", 1) for line in candidates[0].read_text().splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        )
        properties = {key.strip(): value.strip() for key, value in properties.items()}
        if properties.get("Pkg.Revision") != "14" or properties.get("AndroidVersion.ApiLevel") != "31":
            raise ValueError("Unexpected system image revision or API level")
        if not (package / "system.img").is_file() or not (package / "kernel-ranchu").is_file():
            raise ValueError("Incomplete system image package")
        shutil.rmtree(destination)
        shutil.move(str(package), str(destination))
        (evidence / "system-image.json").write_text(json.dumps({
            "api_level": 31, "revision": 14, "abi": "x86_64", "tag": "google_apis",
            "url": url, "archive_sha256": digest.hexdigest(), "archive_bytes": archive.stat().st_size,
        }, indent=2) + "\n")


if __name__ == "__main__":
    main()
