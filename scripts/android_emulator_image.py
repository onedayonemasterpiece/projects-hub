#!/usr/bin/env python3
"""Install the checksum-pinned image into this test profile's dedicated SDK."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.request import urlopen
from zipfile import ZipFile


IMAGE_URL = "https://dl.google.com/android/repository/sys-img/google_apis/x86_64-31_r14.zip"
# Measured in Android run 39112612715, artifact 12131276923/system-image.json.
IMAGE_SHA256 = "7e7081f5784e98dd391ddae52573a75bc1db17a2fd286cb20be46d3eec251f94"
IMAGE_BYTES = 1470822277


def verified_archive(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size != IMAGE_BYTES:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest() == IMAGE_SHA256


def cached_archive(cache: Path) -> tuple[Path, bool]:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"{IMAGE_SHA256}.zip"
    if verified_archive(archive):
        return archive, True
    # A bad cache entry never becomes an input to the SDK. Keep it until a
    # verified replacement is ready; interrupted downloads cannot poison a hit.
    descriptor, name = tempfile.mkstemp(prefix=f"{IMAGE_SHA256}.", suffix=".part", dir=cache)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output, urlopen(IMAGE_URL, timeout=60) as response:
            while block := response.read(1024 * 1024):
                output.write(block)
        if not verified_archive(temporary):
            raise ValueError("System image archive checksum or size does not match the pinned CI receipt")
        os.replace(temporary, archive)
        return archive, False
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    if os.environ.get("PH_ANDROID_DISPOSABLE") != "1":
        raise ValueError("Set PH_ANDROID_DISPOSABLE=1 only for the dedicated test SDK and disposable AVD")
    sdk = Path(os.environ["ANDROID_HOME"]).resolve()
    destination = sdk / "system-images/android-31/google_apis/x86_64"
    evidence = Path(os.environ.get("PH_ANDROID_EVIDENCE_DIR", "android/smoke-evidence"))
    evidence.mkdir(parents=True, exist_ok=True)
    cache = Path(os.environ.get("PH_ANDROID_IMAGE_CACHE", str(
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "projects-hub/android-images")))
    archive, cache_hit = cached_archive(cache)
    # The action has already created its AVD. Its emulator launch uses -wipe-data
    # so the guest userdata matches this exact system image. No SDK install runs
    # after this hook; normal APK compilation still uses Android SDK 35.
    with tempfile.TemporaryDirectory(prefix="ph-ci-api31-", dir=os.environ.get("RUNNER_TEMP") or None) as temporary:
        root = Path(temporary)
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
        # Google's archive is named r14 but its source.properties declares 12.
        # Verify the actual package metadata, not a revision inferred from its URL.
        if (properties.get("Pkg.Revision") != "12" or properties.get("AndroidVersion.ApiLevel") != "31"
                or properties.get("SystemImage.Abi") != "x86_64"
                or properties.get("SystemImage.TagId") != "google_apis"):
            raise ValueError("Unexpected system image revision or API level")
        if not (package / "system.img").is_file() or not (package / "kernel-ranchu").is_file():
            raise ValueError("Incomplete system image package")
        if destination.is_symlink():
            raise ValueError("Dedicated SDK image destination must not be a symlink")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(package), str(destination))
        (evidence / "system-image.json").write_text(json.dumps({
            "api_level": 31, "archive_revision": 14, "package_revision": 12,
            "abi": "x86_64", "tag": "google_apis",
            "url": IMAGE_URL, "archive_sha256": IMAGE_SHA256, "archive_bytes": archive.stat().st_size,
            "cache_hit": cache_hit, "checksum_verified": True,
        }, indent=2) + "\n")


if __name__ == "__main__":
    main()
