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
            time.sleep(min(5, attempt + 1))
            continue
        break
    if check and last is not None:
        raise subprocess.CalledProcessError(
            last.returncode,
            list(args),
            output=last.stdout,
        )
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
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = "Bearer " + token
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
            retries=12,
        )
        uid = re.search(r"uid:(\d+)", listed)
    package_path = run(
        "adb", "shell", "pm", "path", PACKAGE, timeout=20, retries=12
    ).strip()
    if not version or not uid or not package_path.startswith("package:"):
        raise RuntimeError(
            "Cannot read installed package state: "
            f"version={bool(version)} uid={bool(uid)} path={bool(package_path)}"
        )
    return int(version.group(1)), uid.group(1)


def ui_tree() -> ET.Element:
    for _ in range(15):
        try:
            run("adb", "shell", "uiautomator", "dump", "/sdcard/ph-window.xml", timeout=20)
            raw = run("adb", "exec-out", "cat", "/sdcard/ph-window.xml", timeout=20)
            if raw.lstrip().startswith("<?xml"):
                return ET.fromstring(raw)
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("Cannot obtain Android UI hierarchy")


def bounds_center(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value)
    if not match:
        raise RuntimeError("Invalid UI bounds")
    left, top, right, bottom = map(int, match.groups())
    return (left + right) // 2, (top + bottom) // 2


def find_node(predicate, timeout_seconds: int = 60) -> ET.Element:
    deadline = time.time() + timeout_seconds
    last_texts: list[str] = []
    while time.time() < deadline:
        root = ui_tree()
        nodes = list(root.iter("node"))
        last_texts = [
            node.attrib.get("text", "")
            for node in nodes
            if node.attrib.get("text")
        ][-20:]

        # GitHub's Android emulator images occasionally surface a System UI ANR
        # unrelated to the app under test. Keep the system process alive and
        # continue the same product flow instead of treating that overlay as an
        # application failure.
        wait_node = next(
            (
                node
                for node in nodes
                if node.attrib.get("text") == "Wait"
            ),
            None,
        )
        if wait_node is not None:
            tap_node(wait_node)
            time.sleep(2)
            continue

        for node in nodes:
            if predicate(node.attrib):
                return node
        time.sleep(1)
    raise RuntimeError("UI node not found; visible text tail=" + repr(last_texts))


def tap_node(node: ET.Element) -> None:
    x, y = bounds_center(node.attrib.get("bounds", ""))
    run("adb", "shell", "input", "tap", str(x), str(y))


def wait_version(expected: int, timeout_seconds: int = 150) -> tuple[int, str]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            state = package_state()
            if state[0] == expected:
                return state
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError(f"Package did not reach versionCode={expected}")


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    (old_code, old_release), (new_code, new_release) = releases_pair()
    old_asset = asset(old_release, "projects-hub.apk")
    manifest_asset = asset(new_release, "update.json")

    old_apk = WORK / "previous.apk"
    old_sha = download(old_asset["browser_download_url"], old_apk)
    expected_old_digest = str(old_asset.get("digest") or "").removeprefix("sha256:")
    if expected_old_digest and old_sha != expected_old_digest:
        raise RuntimeError("Previous release APK digest mismatch")

    manifest_path = WORK / "update.json"
    download(manifest_asset["browser_download_url"], manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(manifest["versionCode"]) != new_code:
        raise RuntimeError("Latest release manifest versionCode mismatch")
    if "projects-hub.apk" not in str(manifest["apkUrl"]):
        raise RuntimeError("Latest release manifest APK URL is invalid")

    print(f"self-update pair: {old_release['tag_name']} -> {new_release['tag_name']}")
    run("adb", "start-server", timeout=20, retries=3)
    run("adb", "wait-for-device", timeout=90, retries=6)
    boot_deadline = time.time() + 90
    while time.time() < boot_deadline:
        if run("adb", "shell", "getprop", "sys.boot_completed", check=False, timeout=15).strip() == "1":
            break
        time.sleep(2)
    else:
        raise RuntimeError("Emulator did not become adb-stable after boot")
    run("adb", "install", "-r", str(old_apk), timeout=90, retries=15)
    before = package_state()
    if before[0] != old_code:
        raise RuntimeError(f"Expected installed versionCode={old_code}, got {before[0]}")

    run("adb", "shell", "am", "start", "-W", "-n", ACTIVITY, timeout=30)

    update_button = find_node(
        lambda a: a.get("text", "").startswith("Доступно обновление"),
        timeout_seconds=90,
    )
    tap_node(update_button)
    print("app update button: visible and tapped")

    switch = find_node(
        lambda a: a.get("resource-id") == "android:id/switch_widget"
        or a.get("text") == "Allow from this source",
        timeout_seconds=45,
    )
    if switch.attrib.get("resource-id") == "android:id/switch_widget":
        if switch.attrib.get("checked") != "true":
            tap_node(switch)
    else:
        tap_node(switch)
        time.sleep(1)
        switch2 = find_node(
            lambda a: a.get("resource-id") == "android:id/switch_widget",
            timeout_seconds=15,
        )
        if switch2.attrib.get("checked") != "true":
            tap_node(switch2)
    print("standard unknown-sources permission: enabled through Settings UI")

    run("adb", "shell", "input", "keyevent", "KEYCODE_BACK")
    installer = find_node(
        lambda a: a.get("clickable") == "true"
        and a.get("text", "") in {"Update", "Install", "Обновить", "Установить"},
        timeout_seconds=120,
    )
    tap_node(installer)
    print("Android Package Installer: update confirmed through system UI")

    after = wait_version(new_code)
    if before[1] != after[1]:
        raise RuntimeError("Package UID changed; this was not an in-place update")
    print(
        "SELF_UPDATE_PASS "
        f"versionCode={before[0]}->{after[0]} "
        f"uid_preserved=yes"
    )


if __name__ == "__main__":
    main()
