#!/usr/bin/env python3
"""Prepare the one pinned npm SDK archive for existing Android composite builds.

The archive is retained and reverified on reuse. A source checkout or a foreign
directory is never replaced. This helper does not install Python dependencies.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import time
import urllib.request


REPOSITORY = "https://github.com/onedayonemasterpiece/live-interaction"
PACKAGE = "@onedayonemasterpiece/live-interaction"
ARCHIVE_FILE = ".projects-hub-sdk.tgz"
RECEIPT_FILE = ".projects-hub-sdk.json"
MAX_ARCHIVE = 8 * 1024 * 1024
MAX_UNPACKED = 32 * 1024 * 1024
REQUIRED_FILES = {
    "package.json", "browser/client.js", "python/live_interaction/__init__.py",
    "android/settings.gradle.kts", "android/build.gradle.kts",
    "android/platform/build.gradle.kts", "android/platform/src/main/AndroidManifest.xml",
}
# Gradle may create these after preparation. They cannot supply SDK source files.
GENERATED_DIRS = {"android/.gradle", "android/build", "android/platform/.gradle", "android/platform/build"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON member")
        result[key] = value
    return result


def read_json(raw):
    return json.loads(raw, object_pairs_hook=unique_object)


def read_manifest(path):
    pin = read_json(Path(path).read_bytes())
    require(pin.get("repository") == REPOSITORY and pin.get("artifact_type") == "npm_tgz"
            and pin.get("channel") == "candidate", "Expected the published SDK archive candidate manifest")
    for key in ("ref", "release_commit", "tree"):
        require(re.fullmatch(r"[0-9a-f]{40}", pin.get(key) or ""), "Expected exact SDK source and tree hashes")
    require(pin["ref"] == pin["release_commit"], "SDK source ref and release commit disagree")
    version = pin.get("version") or ""
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9]+", version), "Expected an explicit SDK RC version")
    require(pin.get("release_tag") == "v" + version, "SDK release tag/version mismatch")
    expected = f"{REPOSITORY}/releases/download/v{version}/onedayonemasterpiece-live-interaction-{version}.tgz"
    require(pin.get("archive_url") == expected, "Unexpected SDK archive URL")
    require(re.fullmatch(r"[0-9a-f]{64}", pin.get("archive_sha256") or ""), "Expected an SDK archive SHA-256")
    require(type(pin.get("archive_bytes")) is int and 0 < pin["archive_bytes"] <= MAX_ARCHIVE,
            "SDK archive exceeds the download budget")
    return pin


def bounded_read(stream, expected_bytes):
    result = bytearray()
    deadline = time.monotonic() + 120
    while len(result) <= expected_bytes:
        require(time.monotonic() < deadline, "SDK download exceeded its time budget")
        chunk = stream.read(min(65536, expected_bytes + 1 - len(result)))
        if not chunk:
            break
        result.extend(chunk)
    require(len(result) == expected_bytes, "SDK archive size mismatch")
    return bytes(result)


def archive_contents(data, pin):
    require(len(data) == pin["archive_bytes"], "SDK archive size mismatch")
    require(hashlib.sha256(data).hexdigest() == pin["archive_sha256"], "SDK archive SHA-256 mismatch")
    files, names, total = {}, set(), 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for count, member in enumerate(archive, 1):
            require(count <= 1024, "Too many SDK archive entries")
            name = member.name
            parts = PurePosixPath(name).parts
            require(len(name) <= 1024 and len(parts) >= 2 and parts[0] == "package"
                    and ".." not in parts and "\\" not in name
                    and str(PurePosixPath(name)) == name.rstrip("/"), "Unsafe SDK archive path")
            relative = "/".join(parts[1:])
            require(relative not in names and relative not in {ARCHIVE_FILE, RECEIPT_FILE}, "Duplicate or reserved SDK path")
            names.add(relative)
            require(member.isfile() or member.isdir(), "SDK links and special files are forbidden")
            require(not any(relative == folder or relative.startswith(folder + "/") for folder in GENERATED_DIRS),
                    "Generated build content cannot be a source archive input")
            if member.isdir():
                continue
            total += member.size
            require(0 <= member.size <= MAX_UNPACKED and total <= MAX_UNPACKED, "SDK unpacked size exceeds the budget")
            with archive.extractfile(member) as stream:
                content = stream.read(member.size + 1)
            require(len(content) == member.size, "Incomplete SDK archive member")
            files[relative] = content
    require(REQUIRED_FILES <= files.keys(), "SDK archive is missing Android/browser/Python contracts")
    package = read_json(files["package.json"])
    require(package.get("name") == PACKAGE and package.get("version") == pin["version"], "SDK package name/version mismatch")
    for name in files:
        require(not any(parent.as_posix() in files for parent in PurePosixPath(name).parents), "SDK file/directory path conflict")
    receipt = {
        "schema": "projects-hub-sdk-archive/v1", "artifact_type": "npm_tgz", "repository": REPOSITORY,
        "version": pin["version"], "release_tag": pin["release_tag"], "release_commit": pin["release_commit"],
        "source_tree": pin["tree"], "archive_url": pin["archive_url"], "archive_sha256": pin["archive_sha256"],
        "archive_bytes": len(data), "npm_integrity": "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode(),
        "file_count": len(files), "unpacked_bytes": total,
    }
    return files, receipt


def safe_destination(destination):
    destination = Path(destination).absolute()
    require(not any(path.is_symlink() for path in (destination, *destination.parents)), "SDK destination must not traverse symlinks")
    return destination


def verify_prepared(destination, pin):
    destination = safe_destination(destination)
    require(destination.is_dir() and not (destination / ".git").exists(), "Expected a prepared SDK archive, not a Git checkout")
    for name in (ARCHIVE_FILE, RECEIPT_FILE):
        require((destination / name).is_file() and not (destination / name).is_symlink(), "SDK directory has no verified ownership receipt")
    with (destination / ARCHIVE_FILE).open("rb") as stream:
        files, receipt = archive_contents(bounded_read(stream, pin["archive_bytes"]), pin)
    require(read_json((destination / RECEIPT_FILE).read_bytes()) == receipt, "SDK archive receipt mismatch")
    expected = set(files) | {ARCHIVE_FILE, RECEIPT_FILE}
    for path in destination.rglob("*"):
        require(not path.is_symlink(), "SDK prepared directory contains a symlink")
        name = path.relative_to(destination).as_posix()
        if any(name == folder or name.startswith(folder + "/") for folder in GENERATED_DIRS):
            continue
        require(path.is_dir() or name in expected, "SDK prepared directory has unexpected source files")
    for name, content in files.items():
        path = destination / name
        require(path.is_file() and path.stat().st_size == len(content) and path.read_bytes() == content,
                "Extracted SDK source differs from the pinned archive: " + name)
    return receipt


def prepare(manifest, destination, archive_path=None, verify_only=False):
    pin = read_manifest(manifest)
    destination = safe_destination(destination)
    if destination.exists() or verify_only:
        return verify_prepared(destination, pin)
    if archive_path is not None:
        with Path(archive_path).open("rb") as stream:
            data = bounded_read(stream, pin["archive_bytes"])
    else:
        # Only a fixed public GitHub release URL from the validated manifest;
        # credentials are never read or attached to this download.
        with urllib.request.urlopen(pin["archive_url"], timeout=30) as stream:
            require(stream.geturl().startswith("https://"), "SDK archive redirect must use HTTPS")
            data = bounded_read(stream, pin["archive_bytes"])
    files, receipt = archive_contents(data, pin)
    destination.parent.mkdir(parents=True, exist_ok=True)
    safe_destination(destination)
    # Exclusive directory claim prevents replacing any user's source checkout.
    # After an I/O failure the incomplete directory is refused on reuse; choose
    # another destination. No foreign or partially prepared data is deleted.
    destination.mkdir(exist_ok=False)
    for name, content in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(content)
    with (destination / ARCHIVE_FILE).open("xb") as output:
        output.write(data)
    # Write the ownership receipt only after every verified byte is in place.
    with (destination / RECEIPT_FILE).open("x") as output:
        output.write(json.dumps(receipt, indent=2) + "\n")
    return verify_prepared(destination, pin)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("android/live-interaction-candidate.json"))
    parser.add_argument("--destination", type=Path, default=Path(".dependencies/live-interaction"))
    parser.add_argument("--archive", type=Path, help="Use already downloaded bytes; all checks still apply")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare(args.manifest, args.destination, args.archive, args.verify_only), indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, tarfile.TarError) as failure:
        print("Shared SDK preparation failed: " + str(failure), file=sys.stderr)
        raise SystemExit(1)
