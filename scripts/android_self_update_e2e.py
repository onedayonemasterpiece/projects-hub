from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET


REPO = "onedayonemasterpiece/projects-hub"
PACKAGE = "com.kenigevents.projectshub"
ACTIVITY = PACKAGE + "/.MainActivity"
TAG = "ProjectsHubUpdate"
WORK = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "projects-hub-update-e2e"


def run(
    *args: str,
    check: bool = True,
    timeout: int = 60,
    retries: int | None = None,
) -> str:
    attempts = retries if retries is not None else (10 if args and args[0] == "adb" else 1)
    last_returncode = 1
    last_output = ""
    for attempt in range(max(1, attempts)):
        try:
            completed = subprocess.run(
                list(args),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
            last_returncode = completed.returncode
            last_output = completed.stdout or ""
            if completed.returncode == 0:
                return last_output
        except subprocess.TimeoutExpired as failure:
            last_returncode = 124
            output = failure.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", "replace")
            last_output = str(output)

        if args and args[0] == "adb" and attempt + 1 < attempts:
            subprocess.run(
                ["adb", "start-server"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            time.sleep(min(4, attempt + 1))
            continue
        break

    if check:
        raise subprocess.CalledProcessError(
            last_returncode,
            list(args),
            output=last_output,
        )
    return last_output


def request_json(url: str):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "projects-hub-update-e2e",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def download(url: str, target: Path) -> str:
    digest = hashlib.sha256()
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "projects-hub-update-e2e"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        with target.open("wb") as handle:
            while True:
                chunk = response.read(128 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
    return digest.hexdigest()


def releases_pair():
    releases = request_json(f"https://api.github.com/repos/{REPO}/releases?per_page=20")
    numbered = []
    for release in releases:
        match = re.fullmatch(r"android-v(\d+)", str(release.get("tag_name") or ""))
        if match and not release.get("draft") and not release.get("prerelease"):
            numbered.append((int(match.group(1)), release))
    numbered.sort(key=lambda item: item[0])
    if len(numbered) < 2:
        raise RuntimeError("Need at least two Android releases for update E2E")
    previous, latest = numbered[-2], numbered[-1]
    if latest[0] <= previous[0]:
        raise RuntimeError("Release version codes are not increasing")
    return previous, latest


def asset(release: dict, name: str) -> dict:
    for item in release.get("assets", []):
        if item.get("name") == name:
            return item
    raise RuntimeError(f"Release {release.get('tag_name')} is missing {name}")


def package_state() -> tuple[int, str]:
    output = run("adb", "shell", "dumpsys", "package", PACKAGE, timeout=30)
    version = re.search(r"versionCode=(\d+)", output)
    uid = re.search(r"(?:userId|appId)=(\d+)", output)
    if uid is None:
        listed = run(
            "adb",
            "shell",
            "cmd",
            "package",
            "list",
            "packages",
            "-U",
            PACKAGE,
            check=False,
            timeout=20,
        )
        uid = re.search(r"uid:(\d+)", listed)
    package_path = run("adb", "shell", "pm", "path", PACKAGE, timeout=20).strip()
    if not version or not uid or not package_path.startswith("package:"):
        raise RuntimeError(
            "Cannot read installed package state: "
            f"version={bool(version)} uid={bool(uid)} path={bool(package_path)}"
        )
    return int(version.group(1)), uid.group(1)


def wait_adb_stable(timeout_seconds: int = 150) -> None:
    deadline = time.time() + timeout_seconds
    consecutive = 0
    while time.time() < deadline:
        state = run("adb", "get-state", check=False, timeout=10, retries=1).strip()
        boot = run(
            "adb",
            "shell",
            "getprop",
            "sys.boot_completed",
            check=False,
            timeout=10,
            retries=1,
        ).strip()
        if state == "device" and boot == "1":
            consecutive += 1
            if consecutive >= 3:
                return
        else:
            consecutive = 0
            run("adb", "reconnect", check=False, timeout=15, retries=1)
            run("adb", "start-server", check=False, timeout=15, retries=1)
        time.sleep(2)
    raise RuntimeError("Emulator did not become adb-stable")


def wait_android_network(timeout_seconds: int = 120) -> None:
    deadline = time.time() + timeout_seconds
    last = ""
    while time.time() < deadline:
        last = run(
            "adb",
            "shell",
            "ping",
            "-c",
            "1",
            "-W",
            "3",
            "api.github.com",
            check=False,
            timeout=10,
            retries=1,
        )
        if (
            "PING api.github.com (" in last
            or "1 received" in last
            or "1 packets received" in last
            or "bytes from" in last
        ):
            return
        time.sleep(3)
    raise RuntimeError(f"Android emulator network/DNS did not become ready: {last[-800:]!r}")


def update_logs() -> str:
    return run(
        "adb",
        "logcat",
        "-d",
        "-v",
        "brief",
        f"{TAG}:I",
        "*:S",
        check=False,
        timeout=20,
        retries=1,
    )


def wait_text_bounds(target_text: str, timeout_seconds: int = 45) -> tuple[int, int, int, int]:
    deadline = time.time() + timeout_seconds
    last_xml = ""
    while time.time() < deadline:
        run(
            "adb",
            "shell",
            "uiautomator",
            "dump",
            "--compressed",
            "/sdcard/projects-hub-window.xml",
            check=False,
            timeout=20,
            retries=1,
        )
        last_xml = run(
            "adb",
            "exec-out",
            "cat",
            "/sdcard/projects-hub-window.xml",
            check=False,
            timeout=20,
            retries=1,
        )
        try:
            root = ET.fromstring(last_xml)
        except ET.ParseError:
            time.sleep(1)
            continue
        for node in root.iter("node"):
            if str(node.attrib.get("text") or "").casefold() != target_text.casefold():
                continue
            match = re.fullmatch(
                r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                str(node.attrib.get("bounds") or ""),
            )
            if not match:
                continue
            left, top, right, bottom = map(int, match.groups())
            if right > left and bottom > top:
                return left, top, right, bottom
        time.sleep(1)
    raise RuntimeError(
        f"Android UI text {target_text!r} was not found; "
        f"tail={last_xml[-1200:]!r}"
    )


def wait_log(pattern: str, timeout_seconds: int = 120) -> re.Match[str]:
    compiled = re.compile(pattern)
    deadline = time.time() + timeout_seconds
    last = ""
    while time.time() < deadline:
        last = update_logs()
        match = compiled.search(last)
        if match:
            return match
        time.sleep(2)
    tail = "\n".join(last.splitlines()[-30:])
    raise RuntimeError(f"Updater log not observed: {pattern!r}; tail={tail!r}")


def current_focus() -> str:
    output = run(
        "adb",
        "shell",
        "dumpsys",
        "window",
        check=False,
        timeout=20,
        retries=2,
    )
    lines = [
        line.strip()
        for line in output.splitlines()
        if "mCurrentFocus" in line or "mFocusedApp" in line
    ]
    return " | ".join(lines[-4:])


def wait_system_installer(timeout_seconds: int = 120) -> str:
    deadline = time.time() + timeout_seconds
    last = ""
    while time.time() < deadline:
        last = current_focus().lower()
        if "packageinstaller" in last or "permissioncontroller" in last:
            return last
        time.sleep(2)
    raise RuntimeError(f"Android Package Installer did not become focused: {last!r}")


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    (old_code, old_release), (new_code, new_release) = releases_pair()
    old_asset = asset(old_release, "projects-hub.apk")
    new_asset = asset(new_release, "projects-hub.apk")
    manifest_asset = asset(new_release, "update.json")

    old_apk = WORK / "previous.apk"
    new_apk = WORK / "latest.apk"
    old_sha = download(old_asset["browser_download_url"], old_apk)
    new_sha = download(new_asset["browser_download_url"], new_apk)

    expected_old = str(old_asset.get("digest") or "").removeprefix("sha256:")
    expected_new = str(new_asset.get("digest") or "").removeprefix("sha256:")
    if expected_old and old_sha != expected_old:
        raise RuntimeError("Previous release APK digest mismatch")
    if expected_new and new_sha != expected_new:
        raise RuntimeError("Latest release APK digest mismatch")

    manifest_path = WORK / "update.json"
    manifest_sha = download(manifest_asset["browser_download_url"], manifest_path)
    expected_manifest = str(manifest_asset.get("digest") or "").removeprefix("sha256:")
    if expected_manifest and manifest_sha != expected_manifest:
        raise RuntimeError("Latest release manifest digest mismatch")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(manifest["versionCode"]) != new_code:
        raise RuntimeError("Latest release manifest versionCode mismatch")
    if str(manifest.get("sha256") or "") != new_sha:
        raise RuntimeError("Latest release manifest SHA-256 mismatch")
    if str(manifest.get("apkUrl") or "") != new_asset["browser_download_url"]:
        raise RuntimeError("Latest release manifest APK URL mismatch")

    print(f"self-update pair: {old_release['tag_name']} -> {new_release['tag_name']}")

    run("adb", "start-server", timeout=20, retries=3)
    run("adb", "wait-for-device", timeout=90, retries=6)
    wait_adb_stable()
    wait_android_network()

    run("adb", "install", "-r", str(old_apk), timeout=90, retries=15)
    before = package_state()
    if before[0] != old_code:
        raise RuntimeError(f"Expected installed versionCode={old_code}, got {before[0]}")

    run("adb", "logcat", "-c", timeout=20, retries=3)
    run("adb", "shell", "am", "force-stop", PACKAGE, timeout=20, retries=3)
    run("adb", "shell", "am", "start", "-W", "-n", ACTIVITY, timeout=30, retries=3)

    wait_log(rf"update_available versionCode={new_code}\b", timeout_seconds=150)

    # The update prompt is rendered by the *currently installed previous APK*.
    # It cannot emit observability added only in the just-published new APK,
    # so acceptance must inspect and use the real Android dialog itself.
    run("adb", "shell", "input", "keyevent", "KEYCODE_WAKEUP", timeout=15, retries=3)
    run("adb", "shell", "wm", "dismiss-keyguard", check=False, timeout=15, retries=1)
    wait_text_bounds("Доступно обновление Projects Hub", timeout_seconds=60)
    left, top, right, bottom = wait_text_bounds("Обновить", timeout_seconds=60)
    x = (left + right) // 2
    y = (top + bottom) // 2
    print(f"automatic update dialog confirm: ui bounds={left},{top},{right},{bottom}")
    run("adb", "shell", "input", "tap", str(x), str(y), timeout=15, retries=3)
    wait_log(rf"update_download_start versionCode={new_code}\b", timeout_seconds=60)

    permission_required = re.search(
        rf"install_permission_required versionCode={new_code}\b",
        update_logs(),
    )
    if permission_required:
        # Emulator-only acceptance helper. Production continues to require the
        # user's standard Android "Allow from this source" confirmation.
        # The Settings activity launch is asynchronous: wait for it before
        # changing the app-op, otherwise KEYCODE_BACK can race the transition
        # and Projects Hub never receives onResume().
        deadline = time.time() + 45
        last_focus = ""
        while time.time() < deadline:
            last_focus = current_focus()
            if "com.android.settings" in last_focus:
                break
            time.sleep(1)
        else:
            raise RuntimeError(
                "Unknown-app-sources Settings did not become foreground: "
                + repr(last_focus)
            )

        run(
            "adb",
            "shell",
            "appops",
            "set",
            PACKAGE,
            "REQUEST_INSTALL_PACKAGES",
            "allow",
            timeout=20,
            retries=3,
        )
        run(
            "adb",
            "shell",
            "input",
            "keyevent",
            "KEYCODE_BACK",
            timeout=15,
            retries=3,
        )
        # Do not require Projects Hub to remain foreground here. onResume()
        # may immediately resume the pending update and launch PackageInstaller
        # before a polling loop ever observes the app window again. The durable
        # product receipts below are the authoritative transition evidence.

    wait_log(rf"update_download_start versionCode={new_code}\b", timeout_seconds=60)
    wait_log(rf"update_verified versionCode={new_code}\b", timeout_seconds=180)
    wait_log(rf"installer_launch versionCode={new_code}\b", timeout_seconds=30)
    wait_system_installer(timeout_seconds=90)
    print("PACKAGE_INSTALLER_HANDOFF_PASS")

    # Hosted System UI has shown emulator-only ANRs on the final confirmation
    # dialog. The product-owned acceptance boundary is the verified APK handoff.
    # Install that exact already-digest-verified signed APK through adb to prove
    # Android accepts it as a same-signature in-place update.
    run("adb", "install", "-r", str(new_apk), timeout=90, retries=15)
    after = package_state()
    if after[0] != new_code:
        raise RuntimeError(f"Expected updated versionCode={new_code}, got {after[0]}")
    if before[1] != after[1]:
        raise RuntimeError("Package UID changed; this was not an in-place update")

    print(
        "SELF_UPDATE_HANDOFF_PASS "
        f"versionCode={before[0]}->{after[0]} "
        "manifest_sha256_verified=yes "
        "product_update_available=yes "
        "product_dialog_shown=yes "
        "product_dialog_confirmed=yes "
        "product_download_verified=yes "
        "package_installer_handoff=yes "
        "same_signature_in_place_update=yes "
        "uid_preserved=yes "
        "final_human_installer_tap=physical_gate"
    )


if __name__ == "__main__":
    main()
