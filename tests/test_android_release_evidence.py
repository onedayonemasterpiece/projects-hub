"""Adverse synthetic receipts for the signed RC gate; no GitHub/SDK/key access."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SHARED_PIN = json.loads((ROOT / "android/live-interaction-candidate.json").read_text())
SOURCE_SHA, SOURCE_TREE, MERGE_SHA = "a" * 40, "b" * 40, "c" * 40
CERTIFICATE = "d" * 64


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("android_release_gate", ROOT / "scripts/android_release_evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_bytes(entries):
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        for name, value in entries.items():
            archive.writestr(name, value if isinstance(value, (str, bytes)) else json.dumps(value))
    return stream.getvalue()


def synthetic_entries(gate, shared_artifact):
    suite = ET.Element("testsuite", tests=str(len(gate.REQUIRED_TESTS)), failures="0", errors="0", skipped="0")
    for classname, name in sorted(gate.REQUIRED_TESTS):
        ET.SubElement(suite, "testcase", classname=classname, name=name)
    return {
        "smoke-evidence/checkout.json": {
            "repository": gate.REPOSITORY, "run_id": 123, "event_name": "workflow_dispatch",
            "candidate_head_sha": SOURCE_SHA, "checkout_sha": SOURCE_SHA, "checkout_tree": SOURCE_TREE,
            "shared_artifact": shared_artifact,
        },
        "smoke-evidence/prepare-status.json": {"phase": "prepare", "exit_code": 0},
        "smoke-evidence/run-status.json": {"phase": "completed", "exit_code": 0},
        "smoke-evidence/audio-verification.json": {"passed": True, "checks": dict.fromkeys(gate.AUDIO_CHECKS, True)},
        "smoke-evidence/system-image.json": {
            "archive_sha256": gate.IMAGE_SHA256, "archive_bytes": 1470822277, "api_level": 31,
            "package_revision": 12, "abi": "x86_64", "tag": "google_apis", "checksum_verified": True,
        },
        "app/build/outputs/androidTest-results/connected/debug/TEST-device.xml": ET.tostring(suite),
        "app/build/outputs/connected_android_test_additional_output/debug/device/voice-e2e/voice-flow.json": {"passed": True},
    }


@pytest.fixture
def candidate(gate, tmp_path, monkeypatch):
    source = tmp_path / "source"
    for name in ("android/live-interaction-candidate.json", "pyproject.toml", "src/projects_hub/version.py",
                 "web/package.json", "web/package-lock.json"):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    args = SimpleNamespace(source=source, shared=tmp_path / "shared", source_sha=SOURCE_SHA,
                           run_id=123, version_code=64, destination=tmp_path / "dist",
                           github_output=tmp_path / "outputs")
    # A small synthetic SDK exercises the real archive helper; it is never an
    # Android build or evidence about the published package's runtime behavior.
    from prepare_live_framework import prepare as prepare_sdk, REQUIRED_FILES
    pin = deepcopy(SHARED_PIN)
    sdk_stream = io.BytesIO()
    with tarfile.open(fileobj=sdk_stream, mode="w:gz") as archive:
        for name in sorted(REQUIRED_FILES):
            payload = (json.dumps({"name": "@onedayonemasterpiece/live-interaction", "version": pin["version"]}).encode()
                       if name == "package.json" else b"synthetic source")
            member = tarfile.TarInfo("package/" + name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    sdk_bytes = sdk_stream.getvalue()
    sdk_archive = tmp_path / "sdk.tgz"
    sdk_archive.write_bytes(sdk_bytes)
    pin.update(archive_sha256=hashlib.sha256(sdk_bytes).hexdigest(), archive_bytes=len(sdk_bytes))
    pin_path = source / "android/live-interaction-candidate.json"
    pin_path.write_text(json.dumps(pin))
    shared_artifact = prepare_sdk(pin_path, args.shared, sdk_archive)
    lock_path = source / "web/package-lock.json"
    lock = json.loads(lock_path.read_text())
    lock["packages"]["node_modules/@onedayonemasterpiece/live-interaction"]["integrity"] = shared_artifact["npm_integrity"]
    lock_path.write_text(json.dumps(lock))
    entries = synthetic_entries(gate, shared_artifact)
    reference_bytes = b"synthetic APK anchor; not executable"
    monkeypatch.setattr(gate, "REFERENCE_SHA256", hashlib.sha256(reference_bytes).hexdigest())
    monkeypatch.setattr(gate, "REFERENCE_BYTES", len(reference_bytes))
    run = {"id": 123, "repository": {"full_name": gate.REPOSITORY},
           "head_repository": {"full_name": gate.REPOSITORY}, "path": ".github/workflows/android.yml",
           "status": "completed", "conclusion": "success", "event": "workflow_dispatch",
           "head_sha": SOURCE_SHA, "run_attempt": 2, "run_started_at": "2026-10-18T10:00:00Z"}
    reference = {"id": gate.REFERENCE_RELEASE_ID, "tag_name": "android-v63", "target_commitish": gate.REFERENCE_SOURCE,
                 "draft": False, "prerelease": False, "assets": [{"name": "projects-hub.apk", "id": gate.REFERENCE_ASSET_ID,
                 "size": len(reference_bytes), "digest": "sha256:" + gate.REFERENCE_SHA256, "state": "uploaded"}]}
    artifact = {"id": 456, "name": gate.ARTIFACT_NAME, "expired": False, "created_at": "2026-10-18T10:01:00Z",
                "workflow_run": {"id": 123, "head_sha": SOURCE_SHA, "repository_id": gate.REPOSITORY_ID,
                                 "head_repository_id": gate.REPOSITORY_ID}}
    prefix = gate.REPOSITORY
    responses = {
        f"{prefix}/git/commits/{SOURCE_SHA}": {"sha": SOURCE_SHA, "tree": {"sha": SOURCE_TREE}},
        f"{prefix}/git/commits/{MERGE_SHA}": {"sha": MERGE_SHA, "tree": {"sha": SOURCE_TREE}},
        f"{gate.SHARED_REPOSITORY}/git/commits/{SHARED_PIN['ref']}": {"sha": SHARED_PIN["ref"], "tree": {"sha": SHARED_PIN["tree"]}},
        f"{gate.SHARED_REPOSITORY}/git/ref/tags/{pin['release_tag']}": {"object": {"type": "commit", "sha": pin["release_commit"]}},
        f"{gate.SHARED_REPOSITORY}/releases/tags/{pin['release_tag']}": {
            "tag_name": pin["release_tag"], "prerelease": True, "draft": False,
            "assets": [{"browser_download_url": pin["archive_url"], "state": "uploaded",
                        "size": pin["archive_bytes"], "digest": "sha256:" + pin["archive_sha256"]}]},
        f"{prefix}/actions/runs/123": run,
        f"{prefix}/actions/runs/123/attempts/2/jobs?per_page=100&page=1": {"jobs": [
            {"name": name, "conclusion": "success"} for name in ("build", "emulator-voice (API 31)")]},
        f"{prefix}/actions/runs/123/artifacts?per_page=100&page=1": {"artifacts": [artifact]},
        f"{prefix}/git/matching-refs/tags/android-v64": [],
        f"{prefix}/releases?per_page=100&page=1": [reference],
        f"{prefix}/releases/tags/android-v63": reference,
    }
    transfers, calls = {}, []

    def refresh():
        payload = archive_bytes(entries)
        artifact.update(size_in_bytes=len(payload), digest="sha256:" + hashlib.sha256(payload).hexdigest())
        transfers[f"/repos/{prefix}/actions/artifacts/456/zip"] = payload
        transfers[f"/repos/{prefix}/releases/assets/{gate.REFERENCE_ASSET_ID}"] = reference_bytes

    def fake_command(argv):
        calls.append(argv)
        assert argv[0] == "git", "Offline prepare must not invoke an SDK command"
        assert argv[2] != str(args.shared), "An extracted archive must never be treated as a Git checkout"
        return SOURCE_SHA if argv[-1] == "HEAD" else SOURCE_TREE

    def fake_transfer(argv, **kwargs):
        assert argv[:2] == ["gh", "api"]
        media_types = {
            f"/repos/{prefix}/actions/artifacts/456/zip": "application/vnd.github+json",
            f"/repos/{prefix}/releases/assets/{gate.REFERENCE_ASSET_ID}": "application/octet-stream",
        }
        assert argv[2:4] == ["-H", "Accept: " + media_types[argv[-1]]]
        assert kwargs["check"] is True and kwargs["timeout"] == 180
        calls.append(argv)
        kwargs["stdout"].write(transfers[argv[-1]])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gate, "command", fake_command)
    monkeypatch.setattr(gate, "api", lambda path: deepcopy(responses[path]))
    monkeypatch.setattr(gate.subprocess, "run", fake_transfer)
    refresh()
    return SimpleNamespace(args=args, entries=entries, run=run, artifact=artifact, reference=reference,
                           responses=responses, transfers=transfers, calls=calls, refresh=refresh,
                           shared_artifact=shared_artifact, pin=pin)


def test_complete_candidate_provenance_and_honest_scope(gate, candidate):
    gate.prepare(candidate.args)
    receipt = json.loads((candidate.args.destination / "release-evidence.json").read_text())
    assert receipt["source_sha"] == SOURCE_SHA and receipt["source_tree"] == SOURCE_TREE
    assert receipt["versionName"] == "0.1.62+64" and receipt["versionCode"] == 64
    assert receipt["qualification"] == "release_candidate" and len(receipt["instrumentation_cases"]) == 6
    assert receipt["shared_artifact"] == candidate.shared_artifact
    assert receipt["shared_artifact"]["artifact_type"] == "npm_tgz"
    assert receipt["signed_apk_verified"] is False and receipt["reference_verified"] is False
    assert any("90-minute" in gate_name for gate_name in receipt["open_gates"])
    assert all(call[0] in {"git", "gh"} for call in candidate.calls)
    assert f"build_sha={SOURCE_SHA}\n" in candidate.args.github_output.read_text()


def test_pr_merge_is_accepted_only_with_equal_real_tree(gate, candidate):
    candidate.run["event"] = "pull_request"
    checkout = candidate.entries["smoke-evidence/checkout.json"]
    checkout.update(event_name="pull_request", checkout_sha=MERGE_SHA)
    candidate.refresh()
    gate.prepare(candidate.args)
    receipt = json.loads((candidate.args.destination / "release-evidence.json").read_text())
    assert receipt["source_sha"] == SOURCE_SHA and receipt["tested_checkout"]["checkout_sha"] == MERGE_SHA
    with pytest.raises(ValueError, match="tree differs"):
        gate.validate_checkout(checkout, candidate.run, SOURCE_SHA, SOURCE_TREE, "e" * 40, candidate.shared_artifact)


@pytest.mark.parametrize("field,value,error", [
    ("status", "in_progress", "did not pass"), ("conclusion", "failure", "did not pass"),
    ("head_sha", "f" * 40, "head"), ("path", ".github/workflows/android-release.yml", "workflow"),
    ("head_repository", {"full_name": "somebody/projects-hub"}, "Fork"),
    ("event", "workflow_run", "event"),
])
def test_unqualified_run_stops_before_download_or_signing(gate, candidate, field, value, error):
    candidate.run[field] = value
    with pytest.raises(ValueError, match=error):
        gate.prepare(candidate.args)
    assert not candidate.args.destination.exists()
    assert not any(call[0] != "git" for call in candidate.calls)


@pytest.mark.parametrize("field,value,error", [
    ("created_at", "2026-10-18T09:59:59Z", "predates"), ("expired", True, "expired"),
    ("digest", None, "digest"),
])
def test_stale_or_unverifiable_artifact_cannot_authorize_signing(gate, candidate, field, value, error):
    candidate.artifact[field] = value
    with pytest.raises(ValueError, match=error):
        gate.prepare(candidate.args)
    assert not candidate.args.destination.exists()


def test_download_digest_is_checked_before_zip_and_reference(gate, candidate):
    candidate.artifact["digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        gate.prepare(candidate.args)
    assert not (candidate.args.destination / "reference-v63.apk").exists()
    assert not candidate.args.github_output.exists()


@pytest.mark.parametrize("suffix,status", [
    (b" (HTTP 415)\n", "415"), (" (HTTP 403)\r\n", "403"), (b" network failure\n", "unknown"),
])
def test_download_error_reports_only_safe_http_diagnostic(gate, tmp_path, monkeypatch, suffix, status):
    private_detail = "https://example.invalid/redirect?sig=private-signature token=private-token"

    def reject(argv, **kwargs):
        assert argv[2:4] == ["-H", "Accept: application/vnd.github+json"]
        assert kwargs["check"] is True and kwargs["timeout"] == 180
        stderr = private_detail.encode() + suffix if isinstance(suffix, bytes) else private_detail + suffix
        raise subprocess.CalledProcessError(1, argv, stderr=stderr)

    monkeypatch.setattr(gate.subprocess, "run", reject)
    with pytest.raises(ValueError) as failure:
        gate.download_api(gate.REPOSITORY + "/actions/artifacts/456/zip", tmp_path / "artifact.zip",
                          hashlib.sha256(b"zip").hexdigest(), 3, 3)
    assert str(failure.value) == f"GitHub artifact download failed (endpoint=actions_artifact, exit=1, HTTP={status})"
    assert failure.value.__suppress_context__ is True and failure.value.__cause__ is None
    assert private_detail not in str(failure.value)


def test_download_timeout_does_not_expose_subprocess_details(gate, tmp_path, monkeypatch):
    def timeout(argv, **kwargs):
        assert argv[2:4] == ["-H", "Accept: application/octet-stream"]
        assert kwargs["check"] is True and kwargs["timeout"] == 180
        raise subprocess.TimeoutExpired(argv, 180, stderr=b"https://example.invalid/?sig=private-signature")

    monkeypatch.setattr(gate.subprocess, "run", timeout)
    with pytest.raises(ValueError) as failure:
        gate.download_api(gate.REPOSITORY + "/releases/assets/123", tmp_path / "reference.apk",
                          hashlib.sha256(b"apk").hexdigest(), 3, 3)
    assert str(failure.value) == "GitHub artifact download failed (endpoint=release_asset, reason=timeout, timeout_seconds=180)"
    assert failure.value.__suppress_context__ is True and failure.value.__cause__ is None


def test_download_never_overwrites_existing_evidence(gate, tmp_path, monkeypatch):
    destination = tmp_path / "artifact.zip"
    destination.write_bytes(b"existing evidence")
    monkeypatch.setattr(gate.subprocess, "run", lambda *args, **kwargs: pytest.fail("Must refuse before transfer"))
    with pytest.raises(FileExistsError):
        gate.download_api(gate.REPOSITORY + "/actions/artifacts/456/zip", destination,
                          hashlib.sha256(b"zip").hexdigest(), 3, 3)
    assert destination.read_bytes() == b"existing evidence"


@pytest.mark.parametrize("corruption", ["audio_false", "missing_audio_check", "partial_run", "missing_checkout", "voice_failed", "unchecked_image", "skipped_case", "missing_dedup", "wrong_shared", "old_source_checkout", "unsafe_path"])
def test_partial_or_corrupt_audio_is_never_release_ready(gate, candidate, corruption):
    entries = candidate.entries
    if corruption == "audio_false":
        entries["smoke-evidence/audio-verification.json"]["checks"]["speaker_waveform_matches_reply"] = False
    elif corruption == "missing_audio_check":
        del entries["smoke-evidence/audio-verification.json"]["checks"]["reconnected"]
    elif corruption == "partial_run":
        entries["smoke-evidence/run-status.json"]["phase"] = "instrumentation"
    elif corruption == "missing_checkout":
        del entries["smoke-evidence/checkout.json"]
    elif corruption == "voice_failed":
        key = next(name for name in entries if name.endswith("voice-flow.json"))
        entries[key]["passed"] = False
    elif corruption == "unchecked_image":
        entries["smoke-evidence/system-image.json"]["checksum_verified"] = False
    elif corruption in {"skipped_case", "missing_dedup"}:
        key = next(name for name in entries if name.endswith(".xml"))
        xml = ET.fromstring(entries[key])
        case = next(case for case in xml if "KeepsEveryPublicChange" in case.get("name"))
        if corruption == "skipped_case":
            ET.SubElement(case, "skipped")  # Suite's zero counter must not hide this.
        else:
            xml.remove(case)
        entries[key] = ET.tostring(xml)
    elif corruption == "wrong_shared":
        entries["smoke-evidence/checkout.json"]["shared_artifact"]["archive_sha256"] = "f" * 64
    elif corruption == "old_source_checkout":
        checkout = entries["smoke-evidence/checkout.json"]
        del checkout["shared_artifact"]
        checkout.update(shared_checkout_sha=SHARED_PIN["ref"], shared_checkout_tree=SHARED_PIN["tree"])
    else:
        entries["../not-an-artifact-file"] = "untrusted"
    candidate.refresh()
    with pytest.raises(ValueError):
        gate.prepare(candidate.args)
    assert not (candidate.args.destination / "reference-v63.apk").exists()
    assert not candidate.args.github_output.exists()


def test_conflicting_shared_lock_entry_stops_before_evidence(gate, candidate):
    path = candidate.args.source / "web/package-lock.json"
    lock = json.loads(path.read_text())
    lock["packages"]["node_modules/@onedayonemasterpiece/live-interaction"]["resolved"] = "git+ssh://git@github.com/onedayonemasterpiece/live-interaction.git#" + "e" * 40
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="lockfile"):
        gate.prepare(candidate.args)
    assert not candidate.args.destination.exists()


@pytest.mark.parametrize("tamper", ["npm_integrity", "npm_metadata", "published_archive", "release_tag", "extracted_source"])
def test_archive_binding_is_verified_before_audio_evidence_or_signing(gate, candidate, tamper):
    source = candidate.args.source
    if tamper == "npm_integrity":
        path = source / "web/package-lock.json"
        lock = json.loads(path.read_text())
        lock["packages"]["node_modules/@onedayonemasterpiece/live-interaction"]["integrity"] = "sha512-wrong"
        path.write_text(json.dumps(lock))
    elif tamper == "npm_metadata":
        path = source / "web/package.json"
        package = json.loads(path.read_text())
        package["liveFramework"]["release_commit"] = "e" * 40
        path.write_text(json.dumps(package))
    elif tamper == "published_archive":
        response = candidate.responses[f"{gate.SHARED_REPOSITORY}/releases/tags/{candidate.pin['release_tag']}"]
        response["assets"][0]["digest"] = "sha256:" + "e" * 64
    elif tamper == "release_tag":
        response = candidate.responses[f"{gate.SHARED_REPOSITORY}/git/ref/tags/{candidate.pin['release_tag']}"]
        response["object"]["sha"] = "e" * 40
    else:
        (candidate.args.shared / "browser/client.js").write_text("unreviewed source")
    with pytest.raises(ValueError):
        gate.prepare(candidate.args)
    assert not candidate.args.destination.exists()


@pytest.mark.parametrize("corruption", ["changed_digest", "different_asset", "prerelease_anchor"])
def test_stable_certificate_anchor_cannot_silently_change(gate, candidate, corruption):
    if corruption == "changed_digest":
        candidate.reference["assets"][0]["digest"] = "sha256:" + "e" * 64
    elif corruption == "different_asset":
        candidate.reference["assets"][0]["id"] += 1
    else:
        candidate.reference["prerelease"] = True
    with pytest.raises(ValueError, match="reference"):
        gate.prepare(candidate.args)
    assert not candidate.args.destination.exists()


@pytest.mark.parametrize("releases,refs", [
    ([{"tag_name": "android-v65", "draft": False, "prerelease": True}], []),
    ([{"tag_name": "android-v64", "draft": True}], []),
    ([], [{"ref": "refs/tags/android-v64"}]),
])
def test_older_rerun_and_existing_drafts_tags_are_immutable(gate, releases, refs):
    with pytest.raises(ValueError):
        gate.validate_monotonic(releases, refs, 64)
    gate.validate_monotonic([{"tag_name": "android-v63", "draft": False}], [{"ref": "refs/tags/android-v640"}], 64)


def test_release_listing_checks_every_page(gate, monkeypatch):
    first = [{"tag_name": f"unrelated-{n}", "draft": False} for n in range(100)]
    calls = []
    def answer(path):
        calls.append(path)
        return first if path.endswith("&page=1") else [{"tag_name": "android-v65", "draft": False}]
    monkeypatch.setattr(gate, "api", answer)
    with pytest.raises(ValueError, match="newer"):
        gate.validate_monotonic(gate.pages(gate.REPOSITORY + "/releases"), [], 64)
    assert len(calls) == 2


def apk_commands(gate, monkeypatch, *, certificate=CERTIFICATE, package=None, version="0.1.62+64", code=64, debug=False, extra_attributes=""):
    def answer(argv):
        if argv[0] == "apksigner":
            return "Verifies\nSigner #1 certificate SHA-256 digest: " + certificate + "\n"
        if argv[0] == "aapt":
            return (f"package: name='{package or gate.PACKAGE}' versionCode='{code}' versionName='{version}' "
                    "platformBuildVersionName='15' compileSdkVersion='35' compileSdkVersionCodename='15'"
                    + extra_attributes + "\n" + ("application-debuggable\n" if debug else ""))
        assert argv[0] == "git"
        return SOURCE_SHA
    monkeypatch.setattr(gate, "command", answer)


@pytest.mark.parametrize("attribute", ["name", "versionCode", "versionName"])
@pytest.mark.parametrize("different", [False, True])
def test_apk_identity_attributes_must_be_unique(gate, tmp_path, monkeypatch, attribute, different):
    apk = tmp_path / "projects-hub.apk"
    apk.write_bytes(b"synthetic APK")
    values = {"name": gate.PACKAGE, "versionCode": "64", "versionName": "0.1.62+64"}
    value = "conflicting" if different else values[attribute]
    apk_commands(gate, monkeypatch, extra_attributes=f" {attribute}='{value}'")
    with pytest.raises(ValueError, match="Duplicate APK package identity attribute"):
        gate.inspect_apk(apk, Path("aapt"), Path("apksigner"))


@pytest.mark.parametrize("wrong", ["package", "debug", "certificate", "version"])
def test_signed_readback_rejects_wrong_install_identity(gate, tmp_path, monkeypatch, wrong):
    apk = tmp_path / "projects-hub.apk"
    apk.write_bytes(b"synthetic new APK")
    receipt = {"reference_verified": True, "qualification": "release_candidate", "source_sha": SOURCE_SHA,
               "versionCode": 64, "versionName": "0.1.62+64", "signing_reference": {"certificate_sha256": CERTIFICATE}}
    path = tmp_path / "release-evidence.json"
    path.write_text(json.dumps(receipt))
    changes = {"package": gate.PACKAGE + ".debug"} if wrong == "package" else (
        {"debug": True} if wrong == "debug" else {"certificate": "e" * 64} if wrong == "certificate" else {"version": "0.1.62"})
    apk_commands(gate, monkeypatch, **changes)
    with pytest.raises(ValueError):
        gate.finish(SimpleNamespace(receipt=path, apk=apk, source=tmp_path, aapt=Path("aapt"), apksigner=Path("apksigner")))
    assert not (tmp_path / "update.json").exists()
    assert json.loads(path.read_text()).get("signed_apk_verified") is not True


def test_signed_receipt_manifest_and_published_bytes_match(gate, candidate, monkeypatch):
    gate.prepare(candidate.args)
    receipt_path = candidate.args.destination / "release-evidence.json"
    sdk = SimpleNamespace(receipt=receipt_path, aapt=Path("aapt"), apksigner=Path("apksigner"))
    apk_commands(gate, monkeypatch, version="0.1.62", code=63)
    gate.verify_reference(sdk)
    apk = candidate.args.destination / "projects-hub.apk"
    apk.write_bytes(b"synthetic signed candidate, not an Android build")
    apk_commands(gate, monkeypatch)
    gate.finish(SimpleNamespace(**vars(sdk), apk=apk, source=candidate.args.source))
    receipt = gate.verified_release_files(receipt_path)
    assert receipt["apk"]["certificate_sha256"] == CERTIFICATE
    assert receipt["versionName"] == "0.1.62+64"
    release = {"id": 789, "tag_name": "android-v64", "target_commitish": SOURCE_SHA, "prerelease": False, "draft": False,
               "assets": [{"name": name, "id": n, "state": "uploaded", "digest": "sha256:" + gate.digest(apk.parent / name)}
                          for n, name in enumerate(("projects-hub.apk", "update.json", "release-evidence.json"), 1)]}
    candidate.responses.update({
        gate.REPOSITORY + "/releases/tags/android-v64": release,
        gate.REPOSITORY + "/git/ref/tags/android-v64": {"object": {"type": "commit", "sha": SOURCE_SHA}},
        gate.REPOSITORY + "/releases/latest": release,
    })
    gate.verify_published(receipt_path)
    assert json.loads((apk.parent / "publication-readback.json").read_text())["stable_latest_tag"] == "android-v64"
    (apk.parent / "publication-readback.json").unlink()
    candidate.responses[gate.REPOSITORY + "/releases/latest"] = candidate.reference
    with pytest.raises(ValueError, match="latest feed does not point"):
        gate.verify_published(receipt_path)
    assert not (apk.parent / "publication-readback.json").exists()
    candidate.responses[gate.REPOSITORY + "/releases/latest"] = release
    release["prerelease"] = True
    with pytest.raises(ValueError, match="not the selected primary app update"):
        gate.verify_published(receipt_path)
    assert not (apk.parent / "publication-readback.json").exists()
    manifest_path = apk.parent / "update.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["apkUrl"] = "https://example.invalid/changed.apk"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest"):
        gate.verified_release_files(receipt_path)


def test_single_update_channel_checks_source_audio_and_identity_before_secrets():
    path = ROOT / ".github/workflows/android-release.yml"
    raw = path.read_text()
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {"source_sha", "android_run_id"}
    steps = workflow["jobs"]["release"]["steps"]
    prepared = next(n for n, step in enumerate(steps) if " prepare " in step.get("run", ""))
    reference = next(n for n, step in enumerate(steps) if " verify-reference " in step.get("run", ""))
    first_secret = next(n for n, step in enumerate(steps) if "secrets.ANDROID_" in json.dumps(step))
    assert prepared < reference < first_secret
    assert "--clobber" not in raw and "--prerelease" not in raw
    assert "--notes-file dist/release-notes.md --latest" in raw
    assert '--target "$BUILD_SHA"' in raw and "publication-guard" in raw and "verify-published" in raw
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    for step in steps:
        if "run" in step:
            result = subprocess.run(["bash", "-n"], input=step["run"], text=True, capture_output=True, timeout=10)
            assert result.returncode == 0, result.stderr
