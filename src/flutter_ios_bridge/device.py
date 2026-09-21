"""Talk to a USB-connected iPhone from Windows/Linux via pymobiledevice3 (optional extra).

Windows needs Apple's USB driver: install "Apple Devices" (Microsoft Store) or iTunes.
Linux needs usbmuxd running (`sudo apt install usbmuxd`).
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .errors import BridgeError

INSTALL_HINT = ("pymobiledevice3 is not installed. Add it with `pipx inject flutter-ios-bridge-mcp pymobiledevice3` "
                "(or install the package with the [device] extra).")


def _base() -> list[str]:
    if importlib.util.find_spec("pymobiledevice3") is not None:
        return [sys.executable, "-m", "pymobiledevice3"]
    exe = shutil.which("pymobiledevice3")
    if exe:
        return [exe]
    raise BridgeError(INSTALL_HINT)


def _run(args: list[str], timeout: int = 120) -> str:
    try:
        res = subprocess.run(_base() + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BridgeError(f"pymobiledevice3 {' '.join(args)} timed out")
    if res.returncode != 0:
        err = (res.stderr or res.stdout).strip()[-1500:]
        hint = ""
        if "usbmux" in err.lower() or "connectionrefused" in err.lower().replace(" ", ""):
            hint = "\nIs the iPhone plugged in, unlocked and 'Trust This Computer' accepted? On Windows install Apple Devices/iTunes; on Linux run usbmuxd."
        raise BridgeError(f"pymobiledevice3 {' '.join(args)} failed: {err}{hint}")
    return res.stdout


def _udid(udid: Optional[str]) -> list[str]:
    return ["--udid", udid] if udid else []


def list_devices() -> list[dict]:
    out = _run(["usbmux", "list"])
    try:
        devs = json.loads(out)
    except json.JSONDecodeError:
        return [{"raw": out.strip()}]
    return [{
        "udid": d.get("UniqueDeviceID") or d.get("Identifier"),
        "name": d.get("DeviceName"),
        "model": d.get("ProductType"),
        "ios_version": d.get("ProductVersion"),
        "connection": d.get("ConnectionType"),
    } for d in devs]


def install(ipa_path: str, udid: Optional[str]) -> dict:
    p = Path(ipa_path).expanduser()
    if not p.exists():
        raise BridgeError(f"{p} not found")
    if "unsigned" in p.name:
        raise BridgeError("That IPA is unsigned; iOS refuses it. Build a signed ad-hoc/development IPA "
                          "(ci_trigger_build build_type=signed-ipa export_method=ad-hoc) or re-sign it with Sideloadly/AltStore.")
    out = _run(["apps", "install", str(p)] + _udid(udid), timeout=600)
    return {"installed": str(p), "output": out.strip()[-800:]}


def list_apps(udid: Optional[str]) -> list[dict]:
    out = _run(["apps", "list", "--type", "User"] + _udid(udid))
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [{"raw": out[-2000:]}]
    return [{"bundle_id": k, "name": v.get("CFBundleDisplayName") or v.get("CFBundleName"),
             "version": v.get("CFBundleShortVersionString"), "build": v.get("CFBundleVersion")}
            for k, v in data.items()]


def uninstall(bundle_id: str, udid: Optional[str]) -> dict:
    _run(["apps", "uninstall", bundle_id] + _udid(udid))
    return {"uninstalled": bundle_id}


def syslog(seconds: int, match: Optional[str], process: Optional[str], udid: Optional[str], max_lines: int = 400) -> dict:
    args = _base() + ["syslog", "live"] + _udid(udid)
    if match:
        args += ["-mi", match]
    if process:
        args += ["-pn", process]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []
    deadline = time.monotonic() + max(1, min(seconds, 120))
    try:
        import threading

        def reader():
            for ln in proc.stdout:  # type: ignore[union-attr]
                lines.append(ln.rstrip())
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.2)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return {"lines": lines[-max_lines:], "captured": len(lines)}


def info(udid: Optional[str]) -> dict:
    out = _run(["lockdown", "info"] + _udid(udid))
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out[-2000:]}
    keys = ["DeviceName", "ProductType", "ProductVersion", "BuildVersion", "UniqueDeviceID", "CPUArchitecture",
            "DeviceClass", "HardwareModel"]
    return {k: d.get(k) for k in keys}
