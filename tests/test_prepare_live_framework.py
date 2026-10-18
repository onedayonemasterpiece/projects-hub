"""Archive ingestion and source-integrity boundaries; all downloads are offline."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sdk():
    spec = importlib.util.spec_from_file_location("sdk_archive_helper", ROOT / "scripts/prepare_live_framework.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_archive(files, extra=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo("package/" + name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
        if extra is not None:
            archive.addfile(extra, io.BytesIO(b"x" * extra.size))
    return stream.getvalue()


@pytest.fixture
def candidate(sdk, tmp_path):
    pin = json.loads((ROOT / "android/live-interaction-candidate.json").read_text())
    files = {
        "package.json": json.dumps({"name": sdk.PACKAGE, "version": pin["version"]}).encode(),
        "browser/client.js": b"export const fixture = true;",
        "python/live_interaction/__init__.py": b"# fixture",
        "android/settings.gradle.kts": b"// fixture", "android/build.gradle.kts": b"// fixture",
        "android/platform/build.gradle.kts": b"// fixture",
        "android/platform/src/main/AndroidManifest.xml": b"<manifest />",
    }
    archive, manifest, destination = (tmp_path / name for name in ("sdk.tgz", "pin.json", "prepared"))
    def save(payload):
        pin.update(archive_sha256=hashlib.sha256(payload).hexdigest(), archive_bytes=len(payload))
        archive.write_bytes(payload)
        manifest.write_text(json.dumps(pin))
    save(make_archive(files))
    return pin, files, archive, manifest, destination, save


def test_preparation_retains_exact_archive_and_reuses_without_download(sdk, candidate, monkeypatch):
    pin, files, archive, manifest, destination, _ = candidate
    receipt = sdk.prepare(manifest, destination, archive)
    assert receipt["artifact_type"] == "npm_tgz" and receipt["release_commit"] == pin["ref"]
    assert (destination / sdk.ARCHIVE_FILE).read_bytes() == archive.read_bytes()
    assert all((destination / name).read_bytes() == data for name, data in files.items())
    monkeypatch.setattr(sdk.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("Reuse must be offline"))
    assert sdk.prepare(manifest, destination) == receipt
    build = destination / "android/platform/build/generated"
    build.mkdir(parents=True)
    (build / "output.txt").write_text("Gradle output, not SDK source")
    assert sdk.prepare(manifest, destination, verify_only=True) == receipt


@pytest.mark.parametrize("tamper", ["source", "extra_source", "receipt", "archive", "source_symlink"])
def test_existing_prepared_directory_must_still_match_every_source_byte(sdk, candidate, tamper):
    pin, _, archive, manifest, destination, _ = candidate
    sdk.prepare(manifest, destination, archive)
    source = destination / "browser/client.js"
    if tamper == "source":
        source.write_bytes(b"changed")
    elif tamper == "extra_source":
        (destination / "android/platform/unreviewed.gradle").write_text("extra source")
    elif tamper == "receipt":
        path = destination / sdk.RECEIPT_FILE
        value = json.loads(path.read_text())
        value["source_tree"] = "f" * 40
        path.write_text(json.dumps(value))
    elif tamper == "archive":
        (destination / sdk.ARCHIVE_FILE).write_bytes(b"changed")
    else:
        source.unlink()
        source.symlink_to(archive)
    with pytest.raises(ValueError):
        sdk.verify_prepared(destination, pin)


@pytest.mark.parametrize("foreign", ["git", "empty", "symlink"])
def test_foreign_destination_is_never_replaced_or_deleted(sdk, candidate, foreign):
    _, _, archive, manifest, destination, _ = candidate
    if foreign == "symlink":
        destination.symlink_to(manifest.parent, target_is_directory=True)
    else:
        destination.mkdir()
        if foreign == "git":
            (destination / ".git").write_text("owned by somebody else")
    with pytest.raises(ValueError):
        sdk.prepare(manifest, destination, archive)
    assert destination.exists() and manifest.exists() and archive.exists()
    if foreign == "git":
        assert (destination / ".git").read_text() == "owned by somebody else"


@pytest.mark.parametrize("attack", ["absolute", "parent", "alias", "symlink", "duplicate", "generated"])
def test_bad_archive_paths_or_links_stop_before_directory_creation(sdk, candidate, attack):
    _, files, archive, manifest, destination, save = candidate
    names = {"absolute": "/package/outside", "parent": "package/../outside",
             "alias": "package/browser//client.js", "symlink": "package/link",
             "duplicate": "package/browser/client.js", "generated": "package/android/build/unreviewed.gradle"}
    entry = tarfile.TarInfo(names[attack])
    if attack == "symlink":
        entry.type, entry.linkname = tarfile.SYMTYPE, "../../outside"
    else:
        entry.size = 1
    save(make_archive(files, entry))
    with pytest.raises(ValueError):
        sdk.prepare(manifest, destination, archive)
    assert not destination.exists()


def test_archive_and_contract_budgets_are_enforced_before_write(sdk, candidate, monkeypatch):
    _, files, archive, manifest, destination, save = candidate
    save(make_archive({name: data for name, data in files.items() if name != "android/platform/build.gradle.kts"}))
    with pytest.raises(ValueError, match="missing"):
        sdk.prepare(manifest, destination, archive)
    save(make_archive(files))
    monkeypatch.setattr(sdk, "MAX_UNPACKED", 20)
    with pytest.raises(ValueError, match="budget"):
        sdk.prepare(manifest, destination, archive)
    assert not destination.exists()
    for payload in (b"xx", b"xxxx"):
        with pytest.raises(ValueError, match="size"):
            sdk.bounded_read(io.BytesIO(payload), 3)


@pytest.mark.parametrize("field,value", [
    ("archive_url", "https://example.invalid/sdk.tgz"), ("ref", "f" * 40),
    ("artifact_type", "git_source"), ("archive_bytes", 20 * 1024 * 1024), ("release_tag", "main"),
])
def test_manifest_cannot_redirect_or_disguise_the_sdk(sdk, candidate, field, value):
    pin, _, archive, manifest, destination, _ = candidate
    altered = deepcopy(pin)
    altered[field] = value
    manifest.write_text(json.dumps(altered))
    with pytest.raises(ValueError):
        sdk.prepare(manifest, destination, archive)
    assert not destination.exists()
