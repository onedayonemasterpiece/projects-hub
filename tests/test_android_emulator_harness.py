"""Offline checks for immutable inputs and ownership in the existing harness."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]


def image_module():
    spec = importlib.util.spec_from_file_location("android_image_test", ROOT / "scripts/android_emulator_image.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def image_archive(revision="12"):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("x86_64/source.properties", f"Pkg.Revision={revision}\nAndroidVersion.ApiLevel=31\nSystemImage.Abi=x86_64\nSystemImage.TagId=google_apis\n")
        archive.writestr("x86_64/system.img", b"fixture-system")
        archive.writestr("x86_64/kernel-ranchu", b"fixture-kernel")
    return buffer.getvalue()


def configure_image(monkeypatch, tmp_path, payload):
    module = image_module()
    monkeypatch.setattr(module, "IMAGE_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(module, "IMAGE_BYTES", len(payload))
    monkeypatch.setenv("PH_ANDROID_DISPOSABLE", "1")
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path / "sdk"))
    monkeypatch.setenv("PH_ANDROID_IMAGE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("PH_ANDROID_EVIDENCE_DIR", str(tmp_path / "evidence"))
    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    return module


def test_verified_image_cache_reused_with_local_temp_and_absent_sdk(monkeypatch, tmp_path):
    payload = image_archive()
    module = configure_image(monkeypatch, tmp_path, payload)
    downloads = []
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: (downloads.append(args[0]) or io.BytesIO(payload)))
    temporary_dirs = []
    original_temporary = module.tempfile.TemporaryDirectory

    def temporary_directory(*args, **kwargs):
        temporary_dirs.append(kwargs.get("dir"))
        return original_temporary(*args, **kwargs)

    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", temporary_directory)
    module.main()
    module.main()
    receipt = json.loads((tmp_path / "evidence/system-image.json").read_text())
    assert len(downloads) == 1
    assert temporary_dirs == [None, None]
    assert receipt["cache_hit"] is True and receipt["checksum_verified"] is True
    assert receipt["archive_sha256"] == hashlib.sha256(payload).hexdigest()
    assert (tmp_path / "sdk/system-images/android-31/google_apis/x86_64/system.img").read_bytes() == b"fixture-system"
    assert not list((tmp_path / "cache").glob("*.part"))


@pytest.mark.parametrize("failure", ["checksum", "metadata"])
def test_invalid_image_never_replaces_installed_sdk(monkeypatch, tmp_path, failure):
    payload = image_archive("99" if failure == "metadata" else "12")
    module = configure_image(monkeypatch, tmp_path, payload)
    downloaded = bytes([payload[0] ^ 1]) + payload[1:] if failure == "checksum" else payload
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: io.BytesIO(downloaded))
    destination = tmp_path / "sdk/system-images/android-31/google_apis/x86_64"
    destination.mkdir(parents=True)
    (destination / "keep").write_text("previous image")
    with pytest.raises(ValueError, match="checksum|revision"):
        module.main()
    assert (destination / "keep").read_text() == "previous image"
    assert not (tmp_path / "evidence/system-image.json").exists()
    assert not list((tmp_path / "cache").glob("*.part"))


def test_corrupt_cache_published_only_after_a_verified_replacement(monkeypatch, tmp_path):
    payload = image_archive()
    module = configure_image(monkeypatch, tmp_path, payload)
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / f"{module.IMAGE_SHA256}.zip"
    cached.write_bytes(b"bad cache")
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: io.BytesIO(b"bad replacement"))
    with pytest.raises(ValueError, match="checksum"):
        module.cached_archive(cache)
    assert cached.read_bytes() == b"bad cache"
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: io.BytesIO(payload))
    path, hit = module.cached_archive(cache)
    assert path == cached and hit is False
    assert cached.read_bytes() == payload


FAKE_HOST = r'''
import json, os, pathlib, sys
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
log = pathlib.Path(os.environ["PH_TEST_COMMAND_LOG"])
with log.open("a") as out:
    out.write(json.dumps([name, args]) + "\n")
state = pathlib.Path(os.environ["PH_TEST_PULSE_STATE"])
modules = json.loads(state.read_text()) if state.exists() else {}
if name == "pactl":
    if args[:2] == ["list", "short"]:
        for key, value in modules.items():
            if args[2] == "modules":
                print(f"{key}\tmodule-null-sink\t{value}")
            elif args[2] == "sinks":
                sink = next((x.split("=", 1)[1] for x in value.split() if x.startswith("sink_name=")), "foreign")
                print(f"{key}\t{sink}\tmodule-null-sink.c")
    elif args[0] == "load-module":
        if os.environ.get("PH_TEST_FAIL_SECOND_MODULE") == "1" and modules:
            sys.exit(23)
        key = str(100 + len(modules))
        modules[key] = " ".join(args[2:])
        state.write_text(json.dumps(modules))
        print(key)
    elif args[0] == "unload-module":
        del modules[args[1]]
        state.write_text(json.dumps(modules))
    elif args[0].startswith("set-default"):
        sys.exit("Global audio defaults must not change")
elif name == "adb":
    if args == ["devices"]:
        print("List of devices attached\n" + os.environ.get("PH_TEST_ADB_DEVICES", ""))
    elif args[:2] == ["-s", "emulator-5554"]:
        command = args[2:]
        if command == ["shell", "getprop", "ro.kernel.qemu"]:
            print("1")
        elif command == ["shell", "getprop", "ro.build.version.sdk"]:
            print("31")
        elif command == ["shell", "pm", "list", "packages", "--user", "0"]:
            print(os.environ.get("PH_TEST_PACKAGE_INVENTORY",
                "package:android\npackage:com.google.android.gms\npackage:com.google.android.googlequicksearchbox\npackage:com.google.android.webview"))
        elif command[:5] in (["shell", "pm", "disable-user", "--user", "0"], ["shell", "am", "force-stop", "--user", "0"]):
            if command[5:] not in (["com.google.android.gms"], ["com.google.android.googlequicksearchbox"]):
                sys.exit("Package mutation outside the fixed test-profile allowlist")
        elif command[:3] == ["shell", "dumpsys", "package"]:
            print("  User 0: installed=true stopped=" + os.environ.get("PH_TEST_PACKAGE_STOPPED", "true") + " enabled=3")
        else:
            sys.exit("Unexpected selected-device operation")
    else:
        sys.exit("Unexpected device operation")
elif name == "Xvfb":
    sys.exit("Unexpected display operation")
elif name == "pulseaudio" and "--kill" in args:
    sys.exit("Shared daemon must not be killed")
'''


@pytest.fixture
def harness(tmp_path):
    class TestEnvironment(dict):
        def __repr__(self):
            return "<isolated test process environment>"

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    script = repo / "scripts/android_emulator_smoke.sh"
    shutil.copyfile(ROOT / "scripts/android_emulator_smoke.sh", script)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for command in ("pactl", "pulseaudio", "adb", "Xvfb", "curl", "gradle"):
        executable = binaries / command
        executable.write_text(f"#!{sys.executable}\n" + FAKE_HOST)
        executable.chmod(0o755)
    # Cleanup uses Python's pidfd implementation, from this test interpreter.
    (binaries / "python").symlink_to(sys.executable)
    environment = TestEnvironment(os.environ, PATH=f"{binaries}:{os.environ['PATH']}",
        PH_ANDROID_DISPOSABLE="1", ANDROID_HOME=str(tmp_path / "sdk"),
        ANDROID_SERIAL="emulator-5554", PH_ANDROID_EVIDENCE_DIR=str(tmp_path / "evidence"),
        PH_ANDROID_STATE_DIR=str(tmp_path / "state"),
        PH_TEST_COMMAND_LOG=str(tmp_path / "commands.jsonl"), PH_TEST_PULSE_STATE=str(tmp_path / "pulse.json"))

    def run(*args):
        return subprocess.run(["bash", str(script), *args], env=environment, capture_output=True, text=True, timeout=12)

    return tmp_path, environment, run


def command_log(root):
    path = root / "commands.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def run_guest_profile(harness):
    root, environment, _ = harness
    (root / "evidence").mkdir(exist_ok=True)
    # Execute the real Bash helper with the same bounded adb wrapper and guards;
    # no fixture server, emulator, package manager or Gradle is started in this test.
    source = (ROOT / "scripts/android_emulator_smoke.sh").read_text()
    helpers = source.partition("\nprepare() {")[0]
    script = root / "repo/scripts/guest-profile-test.sh"
    script.write_text(helpers + "\nconfigure_disposable_guest\n")
    return subprocess.run(["bash", str(script)], env=environment, capture_output=True, text=True, timeout=12)


def test_guest_profile_changes_only_observed_background_packages_and_verifies_state(harness):
    root, environment, _ = harness
    environment["PH_TEST_ADB_DEVICES"] = "emulator-5554\tdevice"
    result = run_guest_profile(harness)
    assert result.returncode == 0, result.stderr
    receipt = json.loads((root / "evidence/guest-background.json").read_text())
    names = ["com.google.android.gms", "com.google.android.googlequicksearchbox"]
    assert receipt["passed"] is True and receipt["phase"] == "verified"
    assert receipt["serial"] == "emulator-5554" and receipt["user_id"] == 0
    assert receipt["packages"] == [{"package": name, "status": "disabled_and_stopped"} for name in names]
    calls = [args[2:] for name, args in command_log(root) if name == "adb" and args[:2] == ["-s", "emulator-5554"]]
    mutations = [args for args in calls if args[1:3] in (["pm", "disable-user"], ["am", "force-stop"])]
    assert mutations == [command + [name] for name in names for command in (
        ["shell", "pm", "disable-user", "--user", "0"], ["shell", "am", "force-stop", "--user", "0"])]
    assert all(call[-1] != "com.google.android.webview" for call in calls)
    assert not any(name == "gradle" for name, _ in command_log(root))


def test_guest_profile_records_absent_package_without_mutating_a_prefix_match(harness):
    root, environment, _ = harness
    environment["PH_TEST_ADB_DEVICES"] = "emulator-5554\tdevice"
    environment["PH_TEST_PACKAGE_INVENTORY"] = "package:android\npackage:com.google.android.gms\npackage:com.google.android.googlequicksearchbox.other"
    result = run_guest_profile(harness)
    assert result.returncode == 0, result.stderr
    receipt = json.loads((root / "evidence/guest-background.json").read_text())
    assert receipt["passed"] is True
    assert receipt["packages"][1] == {"package": "com.google.android.googlequicksearchbox", "status": "absent"}
    assert not any(args[-1:] == ["com.google.android.googlequicksearchbox"] for _, args in command_log(root))


@pytest.mark.parametrize("failure", ["still_running", "invalid_inventory"])
def test_guest_profile_failure_never_creates_a_success_receipt(harness, failure):
    root, environment, _ = harness
    environment["PH_TEST_ADB_DEVICES"] = "emulator-5554\tdevice"
    if failure == "still_running":
        environment["PH_TEST_PACKAGE_STOPPED"] = "false"
    else:
        environment["PH_TEST_PACKAGE_INVENTORY"] = "Error: package manager unavailable"
    result = run_guest_profile(harness)
    assert result.returncode != 0
    receipt = json.loads((root / "evidence/guest-background.json").read_text())
    assert receipt["passed"] is False
    if failure == "still_running":
        assert receipt["packages"][0]["status"] == "verification_failed"
        assert receipt["packages"][1]["status"] == "not_attempted"
    assert not any(args[-1:] == ["com.google.android.googlequicksearchbox"] for _, args in command_log(root))


def test_guest_profile_refuses_unselected_device_before_package_commands(harness):
    root, environment, _ = harness
    environment["PH_TEST_ADB_DEVICES"] = "emulator-5554\tdevice\nphone-serial\tdevice"
    result = run_guest_profile(harness)
    assert result.returncode != 0
    assert command_log(root) == [["adb", ["devices"]]]
    assert not (root / "evidence/guest-background.json").exists()


def test_failed_prepare_cleans_only_its_created_audio_module(harness):
    root, environment, run = harness
    environment["PH_TEST_FAIL_SECOND_MODULE"] = "1"
    result = run("--prepare")
    assert result.returncode == 23, result.stderr
    assert json.loads((root / "evidence/prepare-status.json").read_text())["exit_code"] == 23
    assert json.loads((root / "pulse.json").read_text()) == {}
    assert not (root / "state/resources.tsv").exists()
    calls = command_log(root)
    assert ["pactl", ["unload-module", "100"]] in calls
    assert not any(args and (args[0].startswith("set-default") or args[0] == "--kill") for _, args in calls)
    assert run("--cleanup").returncode == 0


def test_cleanup_protects_foreign_module_and_reused_pid(harness):
    root, _, run = harness
    owner = "ph_e2e_123_456"
    (root / "state").mkdir()
    (root / "pulse.json").write_text(json.dumps({
        "17": f"sink_name=ph_ci_mic sink_properties=device.description={owner}",
        "18": f"sink_name=ph_ci_speaker sink_properties=device.description={owner}7",
    }))
    own = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])

    def starttime(pid):
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]

    try:
        (root / "state/resources.tsv").write_text(
            f"owner\t{owner}\npulse\t17\npulse\t18\nxvfb\t{own.pid}\t{starttime(own.pid)}\n"
            f"xvfb\t{foreign.pid}\t{int(starttime(foreign.pid)) + 1}\n")
        result = run("--cleanup")
        assert result.returncode == 0, result.stderr
        own.wait(timeout=3)
        assert foreign.poll() is None
        assert list(json.loads((root / "pulse.json").read_text())) == ["18"]
        assert not any(name == "adb" for name, _ in command_log(root))
        assert run("--cleanup").returncode == 0
    finally:
        for process in (own, foreign):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)


@pytest.mark.parametrize("mode", ["--prepare", "run"])
def test_existing_fixture_data_is_refused_without_deletion(harness, mode):
    root, _, run = harness
    previous = root / "evidence/fixture-data"
    previous.mkdir(parents=True)
    source = previous / "recording"
    source.write_bytes(b"preserve previous run")
    result = run(mode)
    assert result.returncode != 0
    assert source.read_bytes() == b"preserve previous run"
    assert command_log(root) == []


@pytest.mark.parametrize("devices", ["phone-serial\tdevice", "emulator-5556\tdevice",
    "emulator-5554\tdevice\nphone-serial\tdevice", "emulator-5554\toffline"])
def test_unknown_or_multiple_devices_fail_before_root_install_and_gradle(harness, devices):
    root, environment, run = harness
    (root / "evidence").mkdir()
    (root / "state").mkdir()
    (root / "state/resources.tsv").write_text("owner\tph_e2e_1_2\n")
    (root / "evidence/prepare-status.json").write_text('{"phase":"prepare","exit_code":0}\n')
    environment["PH_TEST_ADB_DEVICES"] = devices
    result = run()
    assert result.returncode != 0
    assert json.loads((root / "evidence/run-status.json").read_text())["phase"] == "device_validation"
    assert [args for name, args in command_log(root) if name == "adb"] == [["devices"]]
    assert not any(name == "gradle" for name, _ in command_log(root))


def test_kvm_preflight_is_read_only_even_when_unavailable(harness):
    root, _, run = harness
    result = run("--preflight")
    assert result.returncode in (0, 1)
    assert not (root / "evidence").exists()
    assert not (root / "state").exists()
    assert command_log(root) == []
