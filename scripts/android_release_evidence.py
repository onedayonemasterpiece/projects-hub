#!/usr/bin/env python3
"""Validate one existing Android audio run and build an immutable signed RC receipt.

This is not a stable release gate. It never signs, publishes, deploys or calls an
AI provider. The existing workflow owns those explicitly separated operations.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from prepare_live_framework import read_manifest, verify_prepared


REPOSITORY = "onedayonemasterpiece/projects-hub"
REPOSITORY_ID = 1389525226
SHARED_REPOSITORY = "onedayonemasterpiece/live-interaction"
PACKAGE = "com.kenigevents.projectshub"
IMAGE_SHA256 = "7e7081f5784e98dd391ddae52573a75bc1db17a2fd286cb20be46d3eec251f94"
# Public, read-back anchor: releases/tags/android-v63, 2026-10-18.
REFERENCE_RELEASE_ID = 415723841
REFERENCE_ASSET_ID = 650139002
REFERENCE_SHA256 = "ec5ba31cd8a32fb566b74415b6cb59d14b29d2a3863f5889a8f85332c1a5c89f"
REFERENCE_BYTES = 1736797
REFERENCE_SOURCE = "f90840468237c06d6fc48b43c2ed52176f736e2d"
ARTIFACT_NAME = "android-emulator-evidence-api-31"
AUDIO_CHECKS = {
    "speaker_waveform_matches_reply", "live_input_waveform_matches_hello",
    "fresh_input_after_reconnect_matches", "offline_recording_not_executed",
    "compressed_sources_persisted", "binary_compressed_audio_both_directions",
    "reconnected", "controlled_provider_boundary_explicit",
}
SMOKE_CLASS = PACKAGE + ".MainActivitySmokeTest"
VOICE_CLASS = PACKAGE + ".NativeVoiceFlowTest"
REQUIRED_TESTS = {(SMOKE_CLASS, name) for name in (
    "activityLaunches", "completionSchedulingCannotBreakStartupOrReopen",
    "debugUpdaterDiscardsStableOffersAtEveryEntryPoint",
    "trustedPageAppliesBothPalettesWithoutReplacingWebView",
    "nativeVoicePublicationKeepsEveryPublicChangeAndCurrentSnapshot",
)} | {(VOICE_CLASS, "actualUiVoiceCapturePlaybackOutageRecoveryAndStop")}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "Expected a full lowercase Git SHA")
    return value


def positive(value):
    require(type(value) is int and value > 0, "Expected a positive integer")
    return value


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON member")
        result[key] = value
    return result


def read_json(value):
    return json.loads(value, object_pairs_hook=json_object)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def command(args):
    # Do not echo subprocess stderr: signing credentials must never reach a receipt.
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, timeout=90).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Required command failed: " + Path(args[0]).name) from exc


def api(path):
    return read_json(command(["gh", "api", "-H", "Accept: application/vnd.github+json", "/repos/" + path]))


def pages(path, member=None):
    result = []
    for page in range(1, 101):
        payload = api(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
        items = payload[member] if member else payload
        require(isinstance(items, list), "Invalid paginated GitHub response")
        result.extend(items)
        if len(items) < 100:
            return result
    raise ValueError("GitHub listing exceeded the bounded page limit")


def download_api(path, destination, expected_sha, expected_bytes, limit):
    require(type(expected_bytes) is int and 0 < expected_bytes <= limit, "Artifact size is outside the download budget")
    require(re.fullmatch(r"[0-9a-f]{64}", expected_sha or ""), "Artifact SHA-256 is missing")
    destination = Path(destination)
    with destination.open("xb") as output:
        try:
            subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream", "/repos/" + path],
                           stdout=output, stderr=subprocess.PIPE, check=True, timeout=180)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ValueError("GitHub artifact download failed") from exc
    require(destination.stat().st_size == expected_bytes, "Downloaded artifact size mismatch")
    require(digest(destination) == expected_sha, "Downloaded artifact SHA-256 mismatch")


def commit_tree(repository, revision):
    payload = api(repository + "/git/commits/" + sha(revision))
    require(payload.get("sha") == revision, "GitHub commit identity mismatch")
    return sha(payload["tree"]["sha"])


def validate_run(run, source_sha, run_id):
    require(run.get("id") == run_id, "Android run ID mismatch")
    require(run.get("repository", {}).get("full_name") == REPOSITORY, "Android run repository mismatch")
    require(run.get("head_repository", {}).get("full_name") == REPOSITORY, "Fork evidence cannot authorize signing")
    require(run.get("path") == ".github/workflows/android.yml", "Wrong Android workflow")
    require(run.get("status") == "completed" and run.get("conclusion") == "success", "Android run did not pass")
    require(run.get("event") in {"push", "pull_request", "workflow_dispatch"}, "Unsupported Android event")
    require(run.get("head_sha") == source_sha, "Android run head is not the selected source SHA")
    positive(run.get("run_attempt"))


def validate_checkout(checkout, run, source_sha, source_tree, actual_tree, shared_artifact):
    require(checkout.get("repository") == REPOSITORY and checkout.get("run_id") == run["id"], "Checkout receipt belongs to another run")
    require(checkout.get("event_name") == run["event"], "Checkout event mismatch")
    require(checkout.get("candidate_head_sha") == source_sha == run["head_sha"], "Candidate head mismatch")
    sha(checkout.get("checkout_sha"))
    require(checkout.get("checkout_tree") == actual_tree == source_tree, "Tested checkout tree differs from the selected source tree")
    if run["event"] != "pull_request":
        require(checkout["checkout_sha"] == source_sha, "Non-PR checkout SHA mismatch")
    require(checkout.get("shared_artifact") == shared_artifact
            and "shared_checkout_sha" not in checkout and "shared_checkout_tree" not in checkout,
            "Android evidence has a different shared SDK artifact; source-checkout evidence cannot qualify an archive build")


def validate_artifact(artifact, run):
    require(artifact.get("name") == ARTIFACT_NAME and artifact.get("expired") is False, "Android evidence is missing or expired")
    owner = artifact.get("workflow_run", {})
    require(owner.get("id") == run["id"] and owner.get("head_sha") == run["head_sha"], "Artifact run identity mismatch")
    require(owner.get("repository_id") == REPOSITORY_ID and owner.get("head_repository_id") == REPOSITORY_ID,
            "Artifact repository identity mismatch")
    created = datetime.fromisoformat(artifact["created_at"].replace("Z", "+00:00"))
    started = datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00"))
    require(created.tzinfo is not None and started.tzinfo is not None and created >= started,
            "Artifact predates the current run attempt")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest") or ""), "Android artifact has no GitHub digest")


def evidence_from_zip(path):
    """Read only bounded receipt members; never extract an Actions artifact."""
    receipts, hashes, tests = {}, {}, set()
    needed = {"checkout.json", "prepare-status.json", "run-status.json", "audio-verification.json", "system-image.json"}
    with ZipFile(path) as archive:
        entries = archive.infolist()
        require(len(entries) <= 10000, "Too many artifact entries")
        require(len({item.filename for item in entries}) == len(entries), "Duplicate artifact member")
        total = 0
        for entry in entries:
            name = entry.filename
            parts = PurePosixPath(name).parts
            require(not name.startswith("/") and ".." not in parts and "\\" not in name, "Unsafe artifact member path")
            require(not stat.S_ISLNK(entry.external_attr >> 16), "Artifact symlinks are forbidden")
            key = name.removeprefix("smoke-evidence/") if name.startswith("smoke-evidence/") else ""
            is_voice = name.startswith("app/build/outputs/connected_android_test_additional_output/") and name.endswith("/voice-e2e/voice-flow.json")
            is_xml = name.startswith("app/build/outputs/androidTest-results/connected/debug/TEST-") and name.endswith(".xml")
            if key not in needed and not is_voice and not is_xml:
                continue
            require(entry.file_size <= 16 * 1024 * 1024, "Receipt exceeds size budget")
            total += entry.file_size
            require(total <= 64 * 1024 * 1024, "Selected evidence exceeds size budget")
            raw = archive.read(entry)
            hashes[name] = hashlib.sha256(raw).hexdigest()
            if is_xml:
                require(b"<!DOCTYPE" not in raw and b"<!ENTITY" not in raw, "XML entities are forbidden")
                xml = ET.fromstring(raw)
                cases = list(xml.iter("testcase"))
                require(cases, "Empty instrumentation XML")
                for suite in xml.iter("testsuite"):
                    require(all(int(suite.get(field, "0")) == 0 for field in ("failures", "errors", "skipped")), "Instrumentation did not fully pass")
                for case in cases:
                    test = (case.get("classname"), case.get("name"))
                    require(test not in tests, "Duplicate instrumentation result")
                    require(not any(child.tag in {"failure", "error", "skipped"} for child in case), "Failed or skipped instrumentation case")
                    tests.add(test)
            else:
                key = "voice-flow.json" if is_voice else key
                require(key not in receipts, "Ambiguous evidence receipt")
                receipts[key] = read_json(raw)
    require(set(receipts) == needed | {"voice-flow.json"}, "Required Android receipts are missing")
    require(REQUIRED_TESTS <= tests, "Required instrumentation cases are missing")
    require(receipts["prepare-status.json"] == {"phase": "prepare", "exit_code": 0}, "Audio prepare did not pass")
    require(receipts["run-status.json"] == {"phase": "completed", "exit_code": 0}, "Full audio run did not complete")
    audio = receipts["audio-verification.json"]
    require(audio.get("passed") is True and AUDIO_CHECKS <= set(audio.get("checks", {}))
            and all(value is True for value in audio["checks"].values()), "Full waveform/compression/recovery checks did not pass")
    require(receipts["voice-flow.json"].get("passed") is True, "Native UI voice flow did not pass")
    image = receipts["system-image.json"]
    require(image.get("archive_sha256") == IMAGE_SHA256 and image.get("archive_bytes") == 1470822277
            and image.get("api_level") == 31 and image.get("package_revision") == 12
            and image.get("abi") == "x86_64" and image.get("tag") == "google_apis"
            and image.get("checksum_verified") is True, "Unverified Android system image")
    return receipts, hashes, sorted(tests)


def validate_monotonic(releases, refs, version_code):
    require(63 < positive(version_code) <= 2100000000, "versionCode must advance the existing release sequence")
    tag = f"android-v{version_code}"
    require(not any(item.get("ref") == "refs/tags/" + tag for item in refs), "Release tag already exists; start a new workflow run")
    for release in releases:
        require(release.get("tag_name") != tag, "Release already exists; assets are immutable")
        match = re.fullmatch(r"android-v([0-9]+)", release.get("tag_name") or "")
        if match and release.get("draft") is False:
            require(int(match[1]) < version_code, "A newer Android version has already been published")


def publication_guard(version_code):
    refs = api(REPOSITORY + f"/git/matching-refs/tags/android-v{version_code}")
    validate_monotonic(pages(REPOSITORY + "/releases"), refs, version_code)


def validate_reference(metadata):
    require(metadata.get("id") == REFERENCE_RELEASE_ID and metadata.get("tag_name") == "android-v63"
            and metadata.get("target_commitish") == REFERENCE_SOURCE
            and metadata.get("draft") is False and metadata.get("prerelease") is False, "Stable signing reference changed")
    matches = [item for item in metadata.get("assets", []) if item.get("name") == "projects-hub.apk"]
    require(len(matches) == 1, "Stable reference APK is ambiguous")
    asset = matches[0]
    require(asset.get("id") == REFERENCE_ASSET_ID and asset.get("size") == REFERENCE_BYTES
            and asset.get("digest") == "sha256:" + REFERENCE_SHA256 and asset.get("state") == "uploaded", "Stable reference APK changed")


def read_pin(source):
    return read_manifest(source / "android/live-interaction-candidate.json")


def validate_shared_release(pin):
    require(commit_tree(SHARED_REPOSITORY, pin["release_commit"]) == pin["tree"], "Shared release source tree mismatch")
    ref = api(SHARED_REPOSITORY + "/git/ref/tags/" + pin["release_tag"])
    require(ref.get("object", {}).get("type") == "commit" and ref["object"].get("sha") == pin["release_commit"],
            "Shared release tag no longer identifies the pinned source")
    release = api(SHARED_REPOSITORY + "/releases/tags/" + pin["release_tag"])
    require(release.get("tag_name") == pin["release_tag"] and release.get("draft") is False
            and release.get("prerelease") is True, "Shared SDK is not the selected published RC")
    matches = [asset for asset in release.get("assets", []) if asset.get("browser_download_url") == pin["archive_url"]]
    require(len(matches) == 1 and matches[0].get("state") == "uploaded"
            and matches[0].get("size") == pin["archive_bytes"]
            and matches[0].get("digest") == "sha256:" + pin["archive_sha256"], "Published shared archive changed")


def prepare(args):
    source_sha, run_id = sha(args.source_sha), positive(args.run_id)
    build_sha = command(["git", "-C", str(args.source), "rev-parse", "HEAD"])
    require(build_sha == source_sha, "Build checkout is not the selected source SHA")
    source_tree = commit_tree(REPOSITORY, source_sha)
    require(command(["git", "-C", str(args.source), "rev-parse", "HEAD^{tree}"]) == source_tree, "Build tree identity mismatch")
    pin = read_pin(args.source)
    shared_sha = pin["ref"]
    shared_artifact = verify_prepared(args.shared, pin)
    validate_shared_release(pin)
    project = tomllib.loads((args.source / "pyproject.toml").read_text())
    require(f"live-interaction[audio] @ git+https://github.com/{SHARED_REPOSITORY}.git@{shared_sha}" in project["project"]["dependencies"], "Python shared pin mismatch")
    web = read_json((args.source / "web/package.json").read_bytes())
    web_spec = pin["archive_url"]
    require(web["dependencies"].get("@onedayonemasterpiece/live-interaction") == web_spec, "Web shared pin mismatch")
    require(web.get("liveFramework") == {
        "repository": pin["repository"], "binding": "browser", "version": pin["version"],
        "ref": shared_sha, "release_commit": pin["release_commit"], "channel": "candidate",
    }, "Web shared release metadata mismatch")
    lock = read_json((args.source / "web/package-lock.json").read_bytes())["packages"]
    locked = lock["node_modules/@onedayonemasterpiece/live-interaction"]
    require(lock[""]["dependencies"].get("@onedayonemasterpiece/live-interaction") == web_spec
            and locked.get("version") == pin["version"]
            and locked.get("resolved") == web_spec
            and locked.get("integrity") == shared_artifact["npm_integrity"], "Web lockfile shared pin mismatch")
    version_file = (args.source / "src/projects_hub/version.py").read_text()
    match = re.search(r'__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', version_file)
    require(match and match[1] == project["project"]["version"], "Product version is inconsistent")
    version_name = f"{match[1]}-rc.{args.version_code}"
    run = api(REPOSITORY + f"/actions/runs/{run_id}")
    validate_run(run, source_sha, run_id)
    jobs = pages(REPOSITORY + f"/actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs", "jobs")
    for name in ("build", "emulator-voice (API 31)"):
        matches = [job for job in jobs if job.get("name") == name]
        require(len(matches) == 1 and matches[0].get("conclusion") == "success", "Required Android job did not pass")
    artifacts = pages(REPOSITORY + f"/actions/runs/{run_id}/artifacts", "artifacts")
    matches = [item for item in artifacts if item.get("name") == ARTIFACT_NAME]
    require(len(matches) == 1, "Android evidence artifact is missing or ambiguous")
    artifact = matches[0]
    validate_artifact(artifact, run)
    publication_guard(args.version_code)
    reference = api(REPOSITORY + "/releases/tags/android-v63")
    validate_reference(reference)
    args.destination.mkdir(parents=True, exist_ok=False)
    archive = args.destination / "android-evidence.zip"
    download_api(REPOSITORY + f"/actions/artifacts/{artifact['id']}/zip", archive,
                 artifact["digest"].removeprefix("sha256:"), artifact["size_in_bytes"], 128 * 1024 * 1024)
    receipts, hashes, tests = evidence_from_zip(archive)
    checkout = receipts["checkout.json"]
    actual_tree = commit_tree(REPOSITORY, checkout["checkout_sha"])
    validate_checkout(checkout, run, source_sha, source_tree, actual_tree, shared_artifact)
    download_api(REPOSITORY + f"/releases/assets/{REFERENCE_ASSET_ID}", args.destination / "reference-v63.apk",
                 REFERENCE_SHA256, REFERENCE_BYTES, REFERENCE_BYTES)
    evidence = {
        "schema": "projects-hub-android-rc/v1", "qualification": "release_candidate", "repository": REPOSITORY,
        "source_sha": build_sha, "source_tree": source_tree, "shared": pin, "shared_artifact": shared_artifact,
        "versionCode": args.version_code, "versionName": version_name, "tag": f"android-v{args.version_code}",
        "package_id": PACKAGE, "android_run_id": run_id, "android_run_attempt": run["run_attempt"],
        "android_run_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        "android_artifact_id": artifact["id"], "android_artifact_sha256": digest(archive),
        "tested_checkout": checkout, "receipt_sha256": hashes, "instrumentation_cases": tests,
        "audio_checks": receipts["audio-verification.json"]["checks"], "system_image": receipts["system-image.json"],
        "signing_reference": {"tag": "android-v63", "release_id": REFERENCE_RELEASE_ID,
                              "asset_id": REFERENCE_ASSET_ID, "apk_sha256": REFERENCE_SHA256},
        "reference_verified": False, "signed_apk_verified": False,
        "scope": "Real emulator audio with controlled AI runner; this workflow does not call a real provider or deploy backend/PWA.",
        "open_gates": ["physical Android handset/background/routes/permissions/signed-update confirmation",
                       "90-minute conversation with renewal/recovery", "physical unstable-mobile-network acceptance",
                       "paired deployed backend/PWA and signed-APK post-release smoke",
                       "four concurrent voices with real Regional Knowledge workload",
                       "KNOWN FAILED primary transcript acceptance (A1/A2): the first Russian final is truncated to 'Слышишь меня?', and the second changes 'соединения' to 'соединений'. A2 verified complete PCM client WebSocket writes; both strict full-phrase checks remain FAILED. This is separate from optional captions.",
                       "stable shared SDK promotion and remaining provider qualification"],
        "captions": "Optional captions remain degraded/unverified; their clock is not promoted by this release.",
    }
    write_json(args.destination / "release-evidence.json", evidence)
    if args.github_output:
        with args.github_output.open("a") as output:
            output.write(f"build_sha={build_sha}\nversion_name={version_name}\ntag={evidence['tag']}\n")


def inspect_apk(path, aapt, apksigner):
    verified = command([str(apksigner), "verify", "--verbose", "--print-certs", str(path)])
    certs = re.findall(r"(?m)^Signer #[0-9]+ certificate SHA-256 digest: ([0-9a-fA-F]{64})\s*$", verified)
    require(len(certs) == 1, "Expected the existing single signing certificate")
    badging = command([str(aapt), "dump", "badging", str(path)])
    package = next((line for line in badging.splitlines() if line.startswith("package: ")), "")
    values = dict(re.findall(r"(name|versionCode|versionName)='([^']*)'", package))
    require(values.get("name") == PACKAGE and values.get("versionCode", "").isdigit(), "APK package identity mismatch")
    require("application-debuggable" not in badging, "Debug APK cannot be a signed RC")
    return {"package_id": values["name"], "versionCode": int(values["versionCode"]),
            "versionName": values.get("versionName"), "certificate_sha256": certs[0].lower(), "sha256": digest(path)}


def verify_reference(args):
    path = args.receipt.parent / "reference-v63.apk"
    require(path.stat().st_size == REFERENCE_BYTES and digest(path) == REFERENCE_SHA256, "Pinned signing reference bytes changed")
    anchor = inspect_apk(path, args.aapt, args.apksigner)
    require(anchor["versionCode"] == 63 and anchor["versionName"] == "0.1.62", "Reference APK version mismatch")
    receipt = read_json(args.receipt.read_bytes())
    receipt["signing_reference"].update(anchor)
    receipt["reference_verified"] = True
    write_json(args.receipt, receipt)


def finish(args):
    receipt = read_json(args.receipt.read_bytes())
    require(receipt.get("reference_verified") is True and receipt.get("qualification") == "release_candidate", "Reference preflight did not pass")
    require(receipt["source_sha"] == command(["git", "-C", str(args.source), "rev-parse", "HEAD"]), "Build source changed after qualification")
    apk = inspect_apk(args.apk, args.aapt, args.apksigner)
    require(apk["versionCode"] == receipt["versionCode"] and apk["versionName"] == receipt["versionName"], "Signed APK version mismatch")
    require(apk["certificate_sha256"] == receipt["signing_reference"]["certificate_sha256"], "Signed APK differs from the existing production key")
    receipt["apk"] = apk
    receipt["signed_apk_verified"] = True
    write_json(args.receipt, receipt)
    manifest = {"versionCode": apk["versionCode"], "versionName": apk["versionName"],
                "apkUrl": f"https://github.com/{REPOSITORY}/releases/download/{receipt['tag']}/projects-hub.apk",
                "sha256": apk["sha256"], "notes": "Projects Hub Android RC " + receipt["source_sha"]}
    write_json(args.receipt.parent / "update.json", manifest)
    notes = [f"# Projects Hub Android {apk['versionName']}", "", "Signed release candidate; full product qualification remains open.", "",
             f"Source: {receipt['source_sha']}", f"Shared SDK: {receipt['shared']['version']} ({receipt['shared']['release_commit']})",
             f"Shared archive: {receipt['shared']['archive_url']}", f"Shared archive SHA-256: {receipt['shared']['archive_sha256']}",
             f"Android audio evidence: {receipt['android_run_url']}", f"APK SHA-256: {apk['sha256']}", "", receipt["scope"], "",
             "## Open acceptance", ""] + ["- " + gate for gate in receipt["open_gates"]]
    notes += ["", receipt["captions"], "", "Install this RC only from its explicit APK link. It is not offered by the stable latest feed.",
              "The production package/key are preserved. This APK does not upgrade the separate .debug package or transfer its local data.",
              "Backend/PWA pairing must be checked before handset use; publishing an APK does not deploy the server.", ""]
    (args.receipt.parent / "release-notes.md").write_text("\n".join(notes), encoding="utf-8")


def verified_release_files(receipt_path):
    receipt = read_json(receipt_path.read_bytes())
    require(receipt.get("signed_apk_verified") is True and receipt.get("qualification") == "release_candidate", "Signed RC verification is missing")
    require(receipt["tag"] == f"android-v{positive(receipt['versionCode'])}", "Release tag/version mismatch")
    require(digest(receipt_path.parent / "projects-hub.apk") == receipt["apk"]["sha256"], "APK changed after verification")
    manifest = read_json((receipt_path.parent / "update.json").read_bytes())
    require(manifest.get("versionCode") == receipt["versionCode"] and manifest.get("versionName") == receipt["versionName"]
            and manifest.get("sha256") == receipt["apk"]["sha256"]
            and manifest.get("apkUrl") == f"https://github.com/{REPOSITORY}/releases/download/{receipt['tag']}/projects-hub.apk",
            "Update manifest differs from the verified signed APK")
    return receipt


def verify_published(receipt_path):
    receipt = verified_release_files(receipt_path)
    published = api(REPOSITORY + "/releases/tags/" + receipt["tag"])
    require(published.get("tag_name") == receipt["tag"] and published.get("target_commitish") == receipt["source_sha"]
            and published.get("prerelease") is True and published.get("draft") is False, "Published release is not the selected RC")
    ref = api(REPOSITORY + "/git/ref/tags/" + receipt["tag"])
    require(ref.get("object", {}).get("type") == "commit" and ref["object"].get("sha") == receipt["source_sha"], "Published tag points to another source")
    assets = {}
    for name in ("projects-hub.apk", "update.json", "release-evidence.json"):
        matches = [asset for asset in published.get("assets", []) if asset.get("name") == name]
        expected = digest(receipt_path.parent / name)
        require(len(matches) == 1 and matches[0].get("state") == "uploaded"
                and matches[0].get("digest") == "sha256:" + expected, "Published asset digest mismatch")
        assets[name] = {"id": matches[0]["id"], "sha256": expected}
    latest = api(REPOSITORY + "/releases/latest")
    require(latest.get("id") != published.get("id") and latest.get("prerelease") is False, "RC appeared in the stable latest feed")
    write_json(receipt_path.parent / "publication-readback.json", {
        "passed": True, "release_id": published["id"], "tag": receipt["tag"], "source_sha": receipt["source_sha"],
        "prerelease": True, "stable_latest_tag": latest["tag_name"], "assets": assets,
        "qualification": "release_candidate", "backend_deployed_by_this_workflow": False,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    prepare_parser = modes.add_parser("prepare")
    for option in ("source", "shared", "destination"):
        prepare_parser.add_argument("--" + option, type=Path, required=True)
    prepare_parser.add_argument("--source-sha", required=True)
    prepare_parser.add_argument("--run-id", type=int, required=True)
    prepare_parser.add_argument("--version-code", type=int, required=True)
    prepare_parser.add_argument("--github-output", type=Path)
    for name in ("verify-reference", "finish"):
        sub = modes.add_parser(name)
        for option in ("receipt", "aapt", "apksigner"):
            sub.add_argument("--" + option, type=Path, required=True)
        if name == "finish":
            sub.add_argument("--apk", type=Path, required=True)
            sub.add_argument("--source", type=Path, required=True)
    for name in ("publication-guard", "verify-published"):
        modes.add_parser(name).add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args)
    elif args.mode == "verify-reference":
        verify_reference(args)
    elif args.mode == "finish":
        finish(args)
    elif args.mode == "verify-published":
        verify_published(args.receipt)
    else:
        receipt = verified_release_files(args.receipt)
        publication_guard(receipt["versionCode"])


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as failure:
        print("Android RC preflight failed: " + str(failure), file=sys.stderr)
        raise SystemExit(1)
