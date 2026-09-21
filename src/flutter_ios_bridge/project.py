"""Inspect and edit the ios/ half of a Flutter project from any OS (no Xcode needed)."""

import json
import plistlib
import re
import struct
from pathlib import Path
from typing import Any, Optional

import yaml

from .config import load_config
from .errors import BridgeError

# plugin name -> (Info.plist keys it needs, extra advice)
PLUGIN_REQUIREMENTS: dict[str, tuple[list[str], str]] = {
    "camera": (["NSCameraUsageDescription", "NSMicrophoneUsageDescription"], ""),
    "image_picker": (["NSPhotoLibraryUsageDescription", "NSCameraUsageDescription", "NSMicrophoneUsageDescription"], ""),
    "file_picker": (["NSPhotoLibraryUsageDescription"], ""),
    "photo_manager": (["NSPhotoLibraryUsageDescription"], ""),
    "image_gallery_saver": (["NSPhotoLibraryAddUsageDescription"], ""),
    "gal": (["NSPhotoLibraryAddUsageDescription"], ""),
    "mobile_scanner": (["NSCameraUsageDescription"], ""),
    "qr_code_scanner": (["NSCameraUsageDescription"], ""),
    "geolocator": (["NSLocationWhenInUseUsageDescription"], "Add NSLocationAlwaysAndWhenInUseUsageDescription too if you request background location."),
    "location": (["NSLocationWhenInUseUsageDescription"], ""),
    "google_maps_flutter": (["NSLocationWhenInUseUsageDescription"], "Call GMSServices.provideAPIKey in AppDelegate.swift."),
    "record": (["NSMicrophoneUsageDescription"], ""),
    "flutter_sound": (["NSMicrophoneUsageDescription"], ""),
    "speech_to_text": (["NSSpeechRecognitionUsageDescription", "NSMicrophoneUsageDescription"], ""),
    "local_auth": (["NSFaceIDUsageDescription"], ""),
    "flutter_blue_plus": (["NSBluetoothAlwaysUsageDescription"], ""),
    "flutter_reactive_ble": (["NSBluetoothAlwaysUsageDescription"], ""),
    "flutter_contacts": (["NSContactsUsageDescription"], ""),
    "contacts_service": (["NSContactsUsageDescription"], ""),
    "device_calendar": (["NSCalendarsUsageDescription"], "iOS 17+: also NSCalendarsFullAccessUsageDescription."),
    "health": (["NSHealthShareUsageDescription", "NSHealthUpdateUsageDescription"], "Enable the HealthKit capability + entitlement."),
    "nfc_manager": (["NFCReaderUsageDescription"], "Needs the NFC Tag Reading capability and com.apple.developer.nfc.readersession.formats entitlement."),
    "app_tracking_transparency": (["NSUserTrackingUsageDescription"], ""),
    "permission_handler": ([], "permission_handler needs the matching PERMISSION_* macros in ios/Podfile post_install, otherwise requests silently return 'denied'."),
    "firebase_messaging": ([], "Enable Push Notifications + Background Modes (remote-notification) and upload an APNs key in Firebase."),
    "flutter_local_notifications": ([], "Set UNUserNotificationCenter delegate in AppDelegate.swift for foreground notifications."),
    "google_sign_in": ([], "Add CFBundleURLTypes with your REVERSED_CLIENT_ID, and GIDClientID to Info.plist."),
    "url_launcher": ([], "List custom schemes you canLaunch() in LSApplicationQueriesSchemes."),
    "webview_flutter": ([], "Plain http:// URLs need NSAppTransportSecurity exceptions."),
    "tflite_flutter": ([], "Needs TensorFlowLiteC via CocoaPods; set platform :ios, '13.0' or higher and test on a real device (simulator arm64 slices can be missing)."),
}

# every usage-description key and what a missing one does
USAGE_KEYS = sorted({k for keys, _ in PLUGIN_REQUIREMENTS.values() for k in keys})

MIN_RECOMMENDED_IOS = (13, 0)


def resolve_project(project_path: Optional[str]) -> Path:
    p = project_path or load_config().project_path
    if not p:
        raise BridgeError("No project_path given and no default configured (set FIB_PROJECT_PATH or [project].path).")
    root = Path(p).expanduser().resolve()
    # allow pointing anywhere inside the project
    for cand in [root, *root.parents]:
        if (cand / "pubspec.yaml").exists():
            root = cand
            break
    else:
        raise BridgeError(f"No pubspec.yaml found at or above {root}")
    if not (root / "ios").is_dir():
        raise BridgeError(f"{root} has no ios/ folder. Run `flutter create --platforms=ios .` in the project first.")
    return root


def paths(root: Path) -> dict[str, Path]:
    ios = root / "ios"
    return {
        "pubspec": root / "pubspec.yaml",
        "ios": ios,
        "info_plist": ios / "Runner" / "Info.plist",
        "pbxproj": ios / "Runner.xcodeproj" / "project.pbxproj",
        "podfile": ios / "Podfile",
        "appicon": ios / "Runner" / "Assets.xcassets" / "AppIcon.appiconset",
        "launch": ios / "Runner" / "Base.lproj" / "LaunchScreen.storyboard",
        "privacy": ios / "Runner" / "PrivacyInfo.xcprivacy",
        "entitlements": ios / "Runner" / "Runner.entitlements",
        "google_service": ios / "Runner" / "GoogleService-Info.plist",
        "appdelegate": ios / "Runner" / "AppDelegate.swift",
    }


# ---------------------------------------------------------------- pubspec

def read_pubspec(root: Path) -> dict[str, Any]:
    with open(root / "pubspec.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def pubspec_version(pub: dict) -> tuple[Optional[str], Optional[str]]:
    v = str(pub.get("version") or "")
    if not v:
        return None, None
    name, _, build = v.partition("+")
    return name or None, build or None


# ---------------------------------------------------------------- Info.plist

def read_info_plist(root: Path) -> dict[str, Any]:
    p = paths(root)["info_plist"]
    if not p.exists():
        raise BridgeError(f"Missing {p}")
    with open(p, "rb") as fh:
        return plistlib.load(fh)


def write_info_plist(root: Path, data: dict[str, Any]) -> None:
    p = paths(root)["info_plist"]
    with open(p, "wb") as fh:
        plistlib.dump(data, fh, sort_keys=False)


def coerce_value(value: Any, value_type: str) -> Any:
    t = value_type.lower()
    if t in ("auto", ""):
        return value
    if t == "string":
        return str(value)
    if t in ("bool", "boolean"):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "y")
    if t in ("int", "integer"):
        return int(value)
    if t in ("real", "float"):
        return float(value)
    if t in ("array", "dict", "json"):
        return json.loads(value) if isinstance(value, str) else value
    raise BridgeError(f"Unknown value_type {value_type!r} (use string/bool/int/real/array/dict/json/auto)")


# ---------------------------------------------------------------- pbxproj

def read_pbxproj(root: Path) -> str:
    p = paths(root)["pbxproj"]
    if not p.exists():
        raise BridgeError(f"Missing {p}")
    return p.read_text(encoding="utf-8")


def _settings(text: str, key: str) -> list[str]:
    return [m.strip().strip('"') for m in re.findall(rf"^\s*{re.escape(key)} = (.+?);\s*$", text, re.M)]


def build_settings(root: Path) -> dict[str, Any]:
    text = read_pbxproj(root)
    ids = _settings(text, "PRODUCT_BUNDLE_IDENTIFIER")
    app_ids = sorted({i for i in ids if not i.endswith(".RunnerTests")})
    return {
        "bundle_identifiers": app_ids,
        "test_bundle_identifiers": sorted({i for i in ids if i.endswith(".RunnerTests")}),
        "deployment_targets": sorted(set(_settings(text, "IPHONEOS_DEPLOYMENT_TARGET"))),
        "development_team": sorted(set(_settings(text, "DEVELOPMENT_TEAM"))),
        "code_sign_style": sorted(set(_settings(text, "CODE_SIGN_STYLE"))),
        "provisioning_profile_specifier": sorted(set(_settings(text, "PROVISIONING_PROFILE_SPECIFIER"))),
        "swift_version": sorted(set(_settings(text, "SWIFT_VERSION"))),
        "entitlements_file": sorted(set(_settings(text, "CODE_SIGN_ENTITLEMENTS"))),
        "marketing_version": sorted(set(_settings(text, "MARKETING_VERSION"))),
        "current_project_version": sorted(set(_settings(text, "CURRENT_PROJECT_VERSION"))),
    }


def main_bundle_id(root: Path) -> Optional[str]:
    ids = build_settings(root)["bundle_identifiers"]
    return ids[0] if len(ids) == 1 else (ids[0] if ids else None)


def set_bundle_identifier(root: Path, new_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+", new_id):
        raise BridgeError(f"{new_id!r} is not a valid reverse-DNS bundle identifier")
    text = read_pbxproj(root)
    old_ids = build_settings(root)["bundle_identifiers"]
    if not old_ids:
        raise BridgeError("No PRODUCT_BUNDLE_IDENTIFIER found in project.pbxproj")
    n = 0
    for old in old_ids:
        for suffix in ("", ".RunnerTests"):
            pat = re.compile(r"(PRODUCT_BUNDLE_IDENTIFIER = )\"?" + re.escape(old + suffix) + r"\"?;")
            text, k = pat.subn(lambda m: f"{m.group(1)}{new_id}{suffix};", text)
            n += k
    paths(root)["pbxproj"].write_text(text, encoding="utf-8")
    return {"old": old_ids, "new": new_id, "replacements": n}


def _parse_ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def podfile_platform(root: Path) -> Optional[str]:
    p = paths(root)["podfile"]
    if not p.exists():
        return None
    m = re.search(r"^\s*platform\s+:ios,\s*['\"]([\d.]+)['\"]", p.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def set_deployment_target(root: Path, version: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d+\.\d+", version):
        raise BridgeError("version must look like 13.0")
    pp = paths(root)
    text = read_pbxproj(root)
    text, n = re.subn(r"(IPHONEOS_DEPLOYMENT_TARGET = )[\d.]+;", rf"\g<1>{version};", text)
    pp["pbxproj"].write_text(text, encoding="utf-8")
    pod_changed = False
    if pp["podfile"].exists():
        pod = pp["podfile"].read_text(encoding="utf-8")
        new_pod, k = re.subn(r"^(\s*)#?\s*platform\s+:ios,\s*['\"][\d.]+['\"]", rf"\1platform :ios, '{version}'", pod, flags=re.M)
        if k == 0:
            new_pod = f"platform :ios, '{version}'\n" + pod
        pp["podfile"].write_text(new_pod, encoding="utf-8")
        pod_changed = True
    # AppFrameworkInfo.plist MinimumOSVersion (older templates)
    afi = pp["ios"] / "Flutter" / "AppFrameworkInfo.plist"
    if afi.exists():
        with open(afi, "rb") as fh:
            d = plistlib.load(fh)
        if "MinimumOSVersion" in d:
            d["MinimumOSVersion"] = version
            with open(afi, "wb") as fh:
                plistlib.dump(d, fh, sort_keys=False)
    return {"pbxproj_replacements": n, "podfile_updated": pod_changed, "version": version,
            "note": "Delete ios/Pods and ios/Podfile.lock (CI does a clean pod install) so pods pick up the new target."}


# ---------------------------------------------------------------- icons

def png_info(path: Path) -> Optional[dict[str, Any]]:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h, depth, color_type = struct.unpack(">IIBB", data[16:26])
    idat = data.find(b"IDAT")
    header = data[:idat] if idat > 0 else data
    has_alpha = color_type in (4, 6) or b"tRNS" in header
    return {"width": w, "height": h, "has_alpha_channel": bool(has_alpha)}


def check_icons(root: Path) -> list[dict]:
    issues: list[dict] = []
    d = paths(root)["appicon"]
    contents = d / "Contents.json"
    if not contents.exists():
        return [issue("error", "icons", "AppIcon.appiconset/Contents.json is missing",
                      "Run `dart run flutter_launcher_icons` or restore the default Flutter icon set.")]
    meta = json.loads(contents.read_text(encoding="utf-8"))
    marketing = None
    for img in meta.get("images", []):
        if img.get("idiom") == "ios-marketing" or img.get("size") == "1024x1024":
            marketing = img
    if not marketing or not marketing.get("filename"):
        issues.append(issue("error", "icons", "No 1024x1024 App Store icon in AppIcon set",
                            "App Store Connect rejects uploads without it."))
        return issues
    info = png_info(d / marketing["filename"])
    if info is None:
        issues.append(issue("error", "icons", f"1024 icon {marketing['filename']} missing or not a PNG", ""))
    else:
        if (info["width"], info["height"]) != (1024, 1024):
            issues.append(issue("error", "icons", f"App Store icon is {info['width']}x{info['height']}, must be 1024x1024", ""))
        if info["has_alpha_channel"]:
            issues.append(issue("error", "icons", "App Store icon has an alpha channel/transparency",
                                "Apple rejects it (ITMS-90717). With flutter_launcher_icons set `remove_alpha_ios: true`."))
    return issues


# ---------------------------------------------------------------- doctor

def issue(severity: str, area: str, message: str, fix: str) -> dict:
    return {"severity": severity, "area": area, "message": message, "fix": fix}


def _appdelegate_has(root: Path, needle: str) -> bool:
    for name in ("AppDelegate.swift", "AppDelegate.m"):
        f = paths(root)["ios"] / "Runner" / name
        if f.exists() and needle in f.read_text(encoding="utf-8", errors="ignore"):
            return True
    return False


def doctor(root: Path) -> dict[str, Any]:
    pp = paths(root)
    issues: list[dict] = []
    pub = read_pubspec(root)
    deps = set((pub.get("dependencies") or {}).keys())

    try:
        plist = read_info_plist(root)
    except BridgeError as e:
        plist = {}
        issues.append(issue("error", "Info.plist", str(e), ""))

    try:
        bs = build_settings(root)
    except BridgeError as e:
        bs = {}
        issues.append(issue("error", "Xcode project", str(e), ""))

    # --- bundle id
    ids = bs.get("bundle_identifiers", [])
    if not ids:
        issues.append(issue("error", "bundle id", "No PRODUCT_BUNDLE_IDENTIFIER found", ""))
    elif len(ids) > 1:
        issues.append(issue("warning", "bundle id", f"Different bundle ids per configuration: {ids}",
                            "Fine for flavors; otherwise use ios_set_bundle_identifier."))
    elif ids[0].startswith("com.example."):
        issues.append(issue("error", "bundle id", f"Bundle id is still {ids[0]}",
                            "Apple won't accept com.example.*. Use ios_set_bundle_identifier."))

    # --- deployment target
    targets = bs.get("deployment_targets", [])
    pod_t = podfile_platform(root)
    if targets and min(_parse_ver(t) for t in targets) < MIN_RECOMMENDED_IOS:
        issues.append(issue("warning", "deployment target", f"IPHONEOS_DEPLOYMENT_TARGET {targets} is below 13.0",
                            "Current Flutter and most plugins need iOS 13+. Use ios_set_deployment_target('13.0')."))
    if pod_t is None and pp["podfile"].exists():
        issues.append(issue("warning", "Podfile", "`platform :ios` is commented out in Podfile",
                            "CocoaPods then guesses a target and plugins may fail to build. Use ios_set_deployment_target."))
    elif pod_t and targets and _parse_ver(pod_t) != max(_parse_ver(t) for t in targets):
        issues.append(issue("warning", "Podfile", f"Podfile platform {pod_t} != Xcode deployment target {targets}",
                            "Keep them equal (ios_set_deployment_target does both)."))

    # --- plugin permission keys
    for dep in sorted(deps):
        if dep in PLUGIN_REQUIREMENTS:
            keys, advice = PLUGIN_REQUIREMENTS[dep]
            missing = [k for k in keys if not str(plist.get(k, "")).strip()]
            if missing:
                issues.append(issue("error", "permissions",
                                    f"{dep} needs {', '.join(missing)} in Info.plist",
                                    "Missing usage strings crash the app on first access and get rejected in review (ITMS-90683). "
                                    "Use ios_set_info_plist_value."))
            if advice and not (dep == "google_maps_flutter" and _appdelegate_has(root, "provideAPIKey")):
                issues.append(issue("info", "plugin", f"{dep}: {advice}", ""))

    # --- firebase
    if any(d.startswith("firebase_") for d in deps) and not pp["google_service"].exists():
        issues.append(issue("warning", "firebase", "firebase_* used but ios/Runner/GoogleService-Info.plist is missing",
                            "Run `flutterfire configure` (works on Windows/Linux) or rely on firebase_options.dart only."))

    # --- misc plist
    if plist and "ITSAppUsesNonExemptEncryption" not in plist:
        issues.append(issue("info", "Info.plist", "ITSAppUsesNonExemptEncryption not set",
                            "Set it to false (if you only use HTTPS) to skip the export-compliance prompt on every TestFlight build."))
    for k in ("CFBundleDisplayName",):
        if plist and k not in plist:
            issues.append(issue("info", "Info.plist", f"{k} missing — home-screen name falls back to CFBundleName", ""))
    if plist and plist.get("NSAppTransportSecurity", {}).get("NSAllowsArbitraryLoads"):
        issues.append(issue("warning", "Info.plist", "NSAllowsArbitraryLoads is true",
                            "App Review asks for a justification; prefer per-domain exceptions."))

    # --- version
    vname, vbuild = pubspec_version(pub)
    if not vname:
        issues.append(issue("warning", "version", "pubspec.yaml has no `version:`", "Add e.g. version: 1.0.0+1"))

    # --- privacy manifest
    if not pp["privacy"].exists():
        issues.append(issue("warning", "privacy manifest", "ios/Runner/PrivacyInfo.xcprivacy not found",
                            "Apple requires a privacy manifest for apps using required-reason APIs (most apps via plugins). "
                            "Add it in Xcode or via the CI Mac; plugin pods ship their own."))

    # --- launch screen, icons
    if not pp["launch"].exists():
        issues.append(issue("error", "launch screen", "LaunchScreen.storyboard missing", "Required for App Store builds."))
    issues.extend(check_icons(root))

    # --- entitlements
    ent = bs.get("entitlements_file") or []
    if ent and not all((pp["ios"] / e).exists() for e in ent):
        issues.append(issue("error", "entitlements", f"CODE_SIGN_ENTITLEMENTS points to missing file {ent}", ""))

    # --- git ignore sanity
    gi = root / ".gitignore"
    if gi.exists() and "Pods" not in gi.read_text(encoding="utf-8", errors="ignore") and not (pp["ios"] / ".gitignore").exists():
        issues.append(issue("info", "git", "ios/Pods not git-ignored", "CI runs pod install; don't commit Pods/."))

    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: order[i["severity"]])
    return {
        "project": str(root),
        "app_name": pub.get("name"),
        "version": vname,
        "build_number": vbuild,
        "bundle_identifiers": ids,
        "deployment_target": targets,
        "podfile_platform": pod_t,
        "development_team": bs.get("development_team"),
        "summary": {s: sum(1 for i in issues if i["severity"] == s) for s in order},
        "issues": issues,
    }
