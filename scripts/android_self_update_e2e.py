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
WORK = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "projects-hub-update-e2e"


def run(
    *args: str,
    check: bool = True,
    timeout: int = 60,
    retries: int | None = None,
) -> str:
    attempts = retries if retries is not None else (12 if args and args[0] == "adb" else 1)
    last = None
    for attempt in range(max(1, attempts)):
        completed = subprocess.run(
            list(args),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        if completed.returncode == 0:
            return completed.stdout
        last = completed
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
    if check and last is not None:
        raise subprocess.CalledProcessError(last.returncode, list(args), output=last.stdout)
    return "" if last is None else last.stdout


def request_json(url: str):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "projects-hub-update-e2e",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        return json.load(response)


def download(url: str, target: Path) -> str:
    headers = {"User-Agent": "projects-hub-update-e2e"}
    digest = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        with target.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 128)
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
    output = run("adb", "shell", "dumpsys", "package", PACKAGE, timeout=30, retries=12)
    version = re.search(r"versionCode=(\d+)", output)
    uid = re.search(r"(?:userId|appId)=(\d+)", output)
    if uid is None:
        listed = run(
            "adb", "shell", "cmd", "package", "list", "packages", "-U", PACKAGE,
            check=False, timeout=20, retries=12,
        )
        uid = re.search(r"uid:(\d+)", listed)
    package_path = run("adb", "shell", "pm", "path", PACKAGE, timeout=20, retries=12).strip()
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
        state = run("adb", "get-state", check=False, timeout=10).strip()
        boot = run(
            "adb", "shell", "getprop", "sys.boot_completed",
            check=False, timeout=10,
        ).strip()
        if state == "device" and boot == "1":
            consecutive += 1
            if consecutive >= 3:
                return
        else:
            consecutive = 0
            run("adb", "reconnect", check=False, timeout=15)
            run("adb", "start-server", check=False, timeout=15)
        time.sleep(2)
    raise RuntimeError("Emulator did not become adb-stable")


def screen_metrics() -> tuple[int, int, float]:
    size = run("adb", "shell", "wm", "size", timeout=15)
    density = run("adb", "shell", "wm", "density", timeout=15)
    size_match = re.search(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", size)
    density_match = re.search(r"(?:Physical|Override) density:\s*(\d+)", density)
    if not size_match or not density_match:
        raise RuntimeError("Cannot read emulator display metrics")
    width, height = map(int, size_match.groups())
    return width, height, int(density_match.group(1)) / 160.0


def current_focus() -> str:
    output = run("adb", "shell", "dumpsys", "window", timeout=20, retries=6)
    lines = [
        line.strip()
        for line in output.splitlines()
        if "mCurrentFocus" in line or "mFocusedApp" in line
    ]
    return " | ".join(lines[-4:])


def _bounds_center(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value)
    if not match:
        return None
    left, top, right, bottom = map(int, match.groups())
    return (left + right) // 2, (top + bottom) // 2


def _ui_nodes() -> list[dict[str, str]]:
    dumped = run(
        "adb",
        "shell",
        "uiautomator",
        "dump",
        "/sdcard/projects-hub-window.xml",
        check=False,
        timeout=20,
        retries=1,
    )
    if "ERROR" in dumped.upper():
        return []
    raw = run(
        "adb",
        "exec-out",
        "cat",
        "/sdcard/projects-hub-window.xml",
        check=False,
        timeout=20,
        retries=1,
    )
    if not raw.lstrip().startswith("<?xml"):
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    return [dict(node.attrib) for node in root.iter("node")]


def tap_update_until_system_ui(width: int, height: int, density: float) -> str:
    # Prefer the actual native Button bounds. Coordinate fallback remains only
    # for a transient UIAutomator failure after the button has already appeared.
    fallback_x = max(1, width - round(110 * density))
    fallback_y = max(1, height - round(46 * density))
    deadline = time.time() + 120
    button_seen = False
    last_texts: list[str] = []
    while time.time() < deadline:
        focus = current_focus().lower()
        if "settings" in focus:
            return "settings"
        if "packageinstaller" in focus or "permissioncontroller" in focus:
            return "installer"

        nodes = _ui_nodes()
        if nodes:
            last_texts = [node.get("text", "") for node in nodes if node.get("text")][-30:]
            update = next(
                (
                    node for node in nodes
                    if node.get("text", "").startswith("Доступно обновление")
                ),
                None,
            )
            if update is not None:
                center = _bounds_center(update.get("bounds", ""))
                if center is None:
                    raise RuntimeError("Update button has invalid bounds")
                button_seen = True
                print("app update button: visible")
                run(
                    "adb", "shell", "input", "tap",
                    str(center[0]), str(center[1]),
                    check=False, timeout=15, retries=1,
                )
                time.sleep(2)
                continue

        if button_seen:
            run(
                "adb", "shell", "input", "tap",
                str(fallback_x), str(fallback_y),
                check=False, timeout=15, retries=1,
            )
        time.sleep(2)

    raise RuntimeError(
        "Update button handoff failed: "
        f"button_seen={button_seen} focus={current_focus()!r} visible={last_texts!r}"
    )


def wait_installer(timeout_seconds: int = 150) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        focus = current_focus().lower()
        if "packageinstaller" in focus or "permissioncontroller" in focus:
            return
        time.sleep(2)
    raise RuntimeError("Projects Hub did not hand off the verified APK to Package Installer")


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
    download(manifest_asset["browser_download_url"], manifest_path)
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

    run("adb", "install", "-r", str(old_apk), timeout=90, retries=15)
    before = package_state()
    if before[0] != old_code:
        raise RuntimeError(f"Expected installed versionCode={old_code}, got {before[0]}")

    run("adb", "shell", "am", "start", "-W", "-n", ACTIVITY, timeout=30, retries=6)
    width, height, density = screen_metrics()
    handoff = tap_update_until_system_ui(width, height, density)
    print(f"app update button: Android handoff={handoff}")

    if handoff == "settings":
        # Test-only shell grant replaces the flaky hosted-emulator tap on the
        # standard "Allow from this source" switch. Production still requires
        # the user's Android Settings confirmation.
        run(
            "adb", "shell", "appops", "set", PACKAGE,
            "REQUEST_INSTALL_PACKAGES", "allow",
            timeout=20, retries=6,
        )
        run("adb", "shell", "input", "keyevent", "KEYCODE_BACK", timeout=15, retries=6)
        wait_installer()

    print("PACKAGE_INSTALLER_HANDOFF_PASS")

    # Do not automate the final human confirmation button on hosted System UI:
    # it is outside product control and has caused emulator-only ANRs. Instead,
    # install the exact same signed/release-digest-verified APK through adb to
    # verify Android accepts it as an in-place update with the same UID.
    run("adb", "install", "-r", str(new_apk), timeout=90, retries=15)
    after = package_state()
    if after[0] != new_code:
        raise RuntimeError(f"Expected updated versionCode={new_code}, got {after[0]}")
    if before[1] != after[1]:
        raise RuntimeError("Package UID changed; this was not an in-place update")

    print(
        "SELF_UPDATE_HANDOFF_PASS "
        f"versionCode={before[0]}->{after[0]} "
        "manifest_sha256_verified=yes package_installer_handoff=yes "
        "same_signature_in_place_update=yes uid_preserved=yes "
        "final_human_installer_tap=physical_gate"
    )


if __name__ == "__main__":
    main()
