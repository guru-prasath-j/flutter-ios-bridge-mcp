"""MCP tool surface for flutter-ios-bridge."""

from typing import Any, Literal, Optional

from . import __version__, asc as asc_mod, device, github_ci, project, remote_mac, signing
from .compat import Image, Server, threaded
from .config import config_path, github_token, load_config
from .errors import BridgeError

INSTRUCTIONS = """\
flutter-ios-bridge lets a Flutter developer on Windows or Linux ship to iOS without owning a Mac.

Backends (use whichever are configured — call bridge_status first):
  1. Local project tools (always available): ios_doctor finds iOS-specific problems (bundle id, permissions
     strings, deployment target, icons, privacy manifest) and ios_* tools fix them by editing Info.plist,
     project.pbxproj and Podfile directly.
  2. GitHub Actions macOS runners: ci_generate_workflow → commit & push → ci_trigger_build → ci_wait_for_run →
     ci_download_artifacts / ci_get_logs. Builds simulator .app, unsigned IPA, or signed IPA (+ TestFlight upload).
  3. App Store Connect API: signing_quick_setup creates certificates (CSR generated locally, no Keychain),
     provisioning profiles and bundle ids, and ci_set_signing_secrets pushes them into GitHub secrets.
     asc_* tools cover devices, profiles, TestFlight builds and beta groups.
  4. Remote Mac over SSH (optional): mac_sync_project, mac_build, mac_run_on_simulator, mac_screenshot (returns
     an image), mac_simulator_logs — a real Simulator loop from Windows/Linux.
  5. USB iPhone (optional, pymobiledevice3): device_list, device_install_app, device_syslog.

Typical first-time flow: bridge_status → ios_doctor → fix errors → ci_generate_workflow → user commits/pushes →
ci_trigger_build(build_type="unsigned-ipa") to prove the project compiles on iOS → signing_quick_setup →
ci_trigger_build(build_type="signed-ipa", testflight=true).
CI builds whatever is pushed to GitHub, so remind the user to commit and push before triggering.
"""

mcp = Server("flutter-ios-bridge", instructions=INSTRUCTIONS)
P = Optional[str]


# ====================================================================== status

@mcp.tool()
@threaded
def bridge_status(project_path: P = None) -> dict:
    """Show which backends (GitHub CI, App Store Connect, remote Mac, USB device) are configured and what is missing."""
    cfg = load_config()
    out: dict[str, Any] = {"version": __version__, "config_file": str(config_path()),
                           "config_file_exists": config_path().exists()}
    try:
        root = project.resolve_project(project_path)
        groot = github_ci.git_root(root)
        out["project"] = {"path": str(root), "git_root": str(groot) if groot else None,
                          "github_repo": cfg.github.repo or (github_ci.repo_from_git(groot) if groot else None),
                          "workflow_present": bool(groot and (groot / ".github" / "workflows" / cfg.github.workflow).exists())}
    except BridgeError as e:
        out["project"] = {"error": str(e)}
    out["github"] = {"token": bool(github_token(cfg)), "workflow": cfg.github.workflow}
    out["app_store_connect"] = {"configured": cfg.asc.configured, "key_id": cfg.asc.key_id}
    out["remote_mac"] = {"configured": cfg.mac.configured, "host": cfg.mac.host}
    try:
        device._base()
        out["usb_device"] = {"pymobiledevice3": True}
    except BridgeError:
        out["usb_device"] = {"pymobiledevice3": False, "hint": device.INSTALL_HINT}
    missing = []
    if not out["github"]["token"]:
        missing.append("GitHub token (GITHUB_TOKEN or `gh auth login`) — needed for cloud macOS builds")
    if not cfg.asc.configured:
        missing.append("App Store Connect API key (ASC_KEY_ID/ASC_ISSUER_ID/ASC_PRIVATE_KEY_PATH) — needed for signing & TestFlight")
    out["missing"] = missing
    return out


# ====================================================================== project

@mcp.tool()
@threaded
def ios_doctor(project_path: P = None) -> dict:
    """Audit the ios/ side of a Flutter project without Xcode: bundle id, Info.plist usage strings required by the
    plugins in pubspec.yaml, deployment target vs Podfile, App Store icon (size/alpha), launch screen, privacy
    manifest, Firebase config, entitlements. Returns issues sorted by severity with fixes."""
    return project.doctor(project.resolve_project(project_path))


@mcp.tool()
@threaded
def ios_get_info_plist(project_path: P = None, keys: Optional[list[str]] = None) -> dict:
    """Read ios/Runner/Info.plist (optionally only the given keys)."""
    data = project.read_info_plist(project.resolve_project(project_path))
    return {k: data.get(k) for k in keys} if keys else data


@mcp.tool()
@threaded
def ios_set_info_plist_value(key: str, value: Any, value_type: str = "auto", project_path: P = None) -> dict:
    """Set a key in ios/Runner/Info.plist. value_type: auto|string|bool|int|real|array|dict|json.
    Example: key='NSCameraUsageDescription', value='Used to scan documents'."""
    root = project.resolve_project(project_path)
    data = project.read_info_plist(root)
    old = data.get(key)
    data[key] = project.coerce_value(value, value_type)
    project.write_info_plist(root, data)
    return {"key": key, "old": old, "new": data[key]}


@mcp.tool()
@threaded
def ios_remove_info_plist_key(key: str, project_path: P = None) -> dict:
    """Delete a key from ios/Runner/Info.plist."""
    root = project.resolve_project(project_path)
    data = project.read_info_plist(root)
    old = data.pop(key, None)
    project.write_info_plist(root, data)
    return {"removed": key, "old": old}


@mcp.tool()
@threaded
def ios_get_build_settings(project_path: P = None) -> dict:
    """Read key Xcode build settings from project.pbxproj (bundle ids, deployment target, team, signing style...)
    plus the Podfile platform."""
    root = project.resolve_project(project_path)
    return {**project.build_settings(root), "podfile_platform": project.podfile_platform(root)}


@mcp.tool()
@threaded
def ios_set_bundle_identifier(bundle_id: str, project_path: P = None) -> dict:
    """Change the iOS bundle identifier (Runner and RunnerTests) in project.pbxproj."""
    return project.set_bundle_identifier(project.resolve_project(project_path), bundle_id)


@mcp.tool()
@threaded
def ios_set_deployment_target(version: str = "13.0", project_path: P = None) -> dict:
    """Set the minimum iOS version consistently in project.pbxproj, Podfile and AppFrameworkInfo.plist."""
    return project.set_deployment_target(project.resolve_project(project_path), version)


# ====================================================================== GitHub Actions

@mcp.tool()
@threaded
def ci_generate_workflow(project_path: P = None, flutter_version: P = None, runner: str = "macos-15",
                         match_local_flutter: bool = True, overwrite: bool = False) -> dict:
    """Write .github/workflows/flutter-ios-bridge.yml (+ a signing helper script) into the repo. The workflow builds
    on a GitHub macOS runner: simulator .app, unsigned IPA, or signed IPA with optional TestFlight upload.
    By default pins the same Flutter version you have installed locally."""
    return github_ci.generate_workflow(project_path, flutter_version, runner, overwrite, match_local_flutter)


@mcp.tool()
@threaded
def ci_trigger_build(build_type: Literal["unsigned-ipa", "simulator", "signed-ipa"] = "unsigned-ipa",
                     export_method: Literal["app-store-connect", "ad-hoc", "release-testing", "development"] = "app-store-connect",
                     testflight: bool = False, ref: P = None, build_name: str = "", build_number: str = "",
                     project_path: P = None, repo: P = None) -> dict:
    """Start an iOS build on GitHub Actions. Builds what is pushed to `ref` (default branch). Returns the run id.
    signed-ipa needs signing secrets (signing_quick_setup); testflight=true also needs ASC secrets."""
    return github_ci.trigger(project_path, build_type, export_method, testflight, ref, build_name, build_number, repo)


@mcp.tool()
@threaded
def ci_list_runs(limit: int = 10, project_path: P = None, repo: P = None) -> list[dict]:
    """List recent runs of the iOS workflow."""
    gh = github_ci.GitHub(project_path=project_path, repo=repo)
    return [github_ci.summarize_run(r) for r in gh.runs(limit)]


@mcp.tool()
@threaded
def ci_wait_for_run(run_id: int, timeout_seconds: int = 240, poll_seconds: int = 20,
                    project_path: P = None, repo: P = None) -> dict:
    """Wait (up to timeout_seconds) for a run to finish; returns status and per-step results. Call repeatedly for
    long builds (a clean iOS build usually takes 8–20 minutes)."""
    return github_ci.wait_for_run(run_id, min(timeout_seconds, 900), poll_seconds, project_path, repo)


@mcp.tool()
@threaded
def ci_get_logs(run_id: int, only_failed: bool = True, tail_lines: int = 120,
                project_path: P = None, repo: P = None) -> dict:
    """Fetch the job logs of a run and extract the Xcode/CocoaPods/Flutter error lines plus the log tail."""
    return github_ci.logs(run_id, only_failed, tail_lines, project_path, repo)


@mcp.tool()
@threaded
def ci_download_artifacts(run_id: int, dest_dir: P = None, project_path: P = None, repo: P = None) -> dict:
    """Download and unzip a run's build outputs (IPA / simulator .app.zip). Default destination:
    <project>/build/ios-bridge/<run_id>/."""
    return github_ci.download_artifacts(run_id, dest_dir, project_path, repo)


@mcp.tool()
@threaded
def ci_cancel_run(run_id: int, project_path: P = None, repo: P = None) -> dict:
    """Cancel a running workflow run."""
    github_ci.GitHub(project_path=project_path, repo=repo).cancel(run_id)
    return {"cancelled": run_id}


def _set_signing_secrets(p12_path: str, p12_password: str, mobileprovision_path: str,
                         include_asc_key: bool, project_path: P, repo: P) -> dict:
    import base64
    from pathlib import Path
    gh = github_ci.GitHub(project_path=project_path, repo=repo)
    signing.inspect_p12(p12_path, p12_password)  # validate before uploading
    prof_bytes = Path(mobileprovision_path).expanduser().read_bytes()
    prof = signing.profile_summary(prof_bytes)
    gh.set_secret("IOS_P12_BASE64", base64.b64encode(Path(p12_path).expanduser().read_bytes()).decode())
    gh.set_secret("IOS_P12_PASSWORD", p12_password)
    gh.set_secret("IOS_PROFILE_BASE64", base64.b64encode(prof_bytes).decode())
    names = ["IOS_P12_BASE64", "IOS_P12_PASSWORD", "IOS_PROFILE_BASE64"]
    if include_asc_key:
        a = gh.cfg.asc
        if not a.configured:
            raise BridgeError("include_asc_key=true but no App Store Connect key is configured.")
        key = a.private_key or Path(a.private_key_path).expanduser().read_text()
        gh.set_secret("ASC_KEY_ID", a.key_id)
        gh.set_secret("ASC_ISSUER_ID", a.issuer_id)
        gh.set_secret("ASC_PRIVATE_KEY", key)
        names += ["ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY"]
    return {"repo": gh.repo, "secrets_set": names, "profile": {k: prof[k] for k in ("name", "bundle_id", "kind", "expires")}}


@mcp.tool()
@threaded
def ci_set_signing_secrets(p12_path: str, p12_password: str, mobileprovision_path: str,
                           include_asc_key: bool = True, project_path: P = None, repo: P = None) -> dict:
    """Upload signing material to the repo's GitHub Actions secrets (encrypted client-side):
    IOS_P12_BASE64, IOS_P12_PASSWORD, IOS_PROFILE_BASE64 and, if include_asc_key, ASC_KEY_ID / ASC_ISSUER_ID /
    ASC_PRIVATE_KEY for TestFlight uploads. Works with a .p12 exported from anywhere, not only signing_quick_setup."""
    return _set_signing_secrets(p12_path, p12_password, mobileprovision_path, include_asc_key, project_path, repo)


# ====================================================================== signing / App Store Connect

@mcp.tool()
@threaded
def signing_quick_setup(bundle_id: P = None, profile_kind: Literal["app-store", "ad-hoc", "development"] = "app-store",
                        app_name: P = None, p12_password: P = None, push_to_github: bool = True,
                        include_asc_key: bool = True, force_new_certificate: bool = False,
                        project_path: P = None, repo: P = None) -> dict:
    """One-shot iOS code signing from Windows/Linux: registers the bundle id, creates (or reuses) an Apple
    certificate from a locally generated CSR, creates a provisioning profile, saves .p12/.mobileprovision under
    ~/.flutter-ios-bridge/signing, and (push_to_github) uploads them as GitHub secrets for signed CI builds.
    bundle_id defaults to the project's. ad-hoc/development include all registered devices."""
    if not bundle_id:
        bundle_id = project.main_bundle_id(project.resolve_project(project_path))
        if not bundle_id:
            raise BridgeError("Pass bundle_id (none found in the project).")
    res = asc_mod.quick_setup(bundle_id, profile_kind, app_name, p12_password, force_new_certificate)
    try:
        root = project.resolve_project(project_path)
        if bundle_id not in project.build_settings(root)["bundle_identifiers"]:
            res["warning"] = f"Project bundle id differs from {bundle_id}; run ios_set_bundle_identifier."
    except BridgeError:
        pass
    if push_to_github:
        res["github"] = _set_signing_secrets(res["p12_path"], res["p12_password"], res["mobileprovision_path"],
                                             include_asc_key, project_path, repo)
    method = {"app-store": "app-store-connect", "ad-hoc": "ad-hoc", "development": "development"}[profile_kind]
    res["next"] = f"ci_trigger_build(build_type='signed-ipa', export_method='{method}'" + \
        (", testflight=True)" if profile_kind == "app-store" else ")")
    return res


@mcp.tool()
@threaded
def signing_inspect(path: str, p12_password: P = None) -> dict:
    """Inspect a .mobileprovision (name, team, bundle id, kind, devices, expiry, certs) or a .p12 (needs password)."""
    from pathlib import Path
    p = Path(path).expanduser()
    if p.suffix.lower() in (".p12", ".pfx"):
        if p12_password is None:
            raise BridgeError("p12_password is required for .p12 files")
        return signing.inspect_p12(str(p), p12_password)
    return signing.profile_summary(p.read_bytes())


@mcp.tool()
@threaded
def asc_list(resource: Literal["apps", "bundle_ids", "devices", "certificates", "profiles"]) -> list[dict]:
    """List App Store Connect / Developer Portal resources."""
    a = asc_mod.ASC()
    if resource == "apps":
        return asc_mod._short(a.apps(), ["name", "bundleId", "sku"])
    if resource == "bundle_ids":
        return asc_mod._short(a.get_all("/v1/bundleIds"), ["identifier", "name", "platform"])
    if resource == "devices":
        return asc_mod._short(a.devices(), ["name", "udid", "model", "status", "platform"])
    if resource == "certificates":
        return asc_mod._short(a.certificates(), ["displayName", "certificateType", "expirationDate", "serialNumber"])
    return asc_mod._short(a.profiles(), ["name", "profileType", "profileState", "expirationDate", "uuid"])


@mcp.tool()
@threaded
def asc_register_bundle_id(identifier: str, name: P = None) -> dict:
    """Register an explicit App ID (bundle identifier) in the Apple Developer portal."""
    b, created = asc_mod.ensure_bundle(asc_mod.ASC(), identifier, name)
    return {"id": b["id"], "identifier": identifier, "created": created}


@mcp.tool()
@threaded
def asc_enable_capability(identifier: str, capability: str) -> dict:
    """Enable a capability on a bundle id, e.g. PUSH_NOTIFICATIONS, ASSOCIATED_DOMAINS, APPLE_ID_AUTH (Sign in with
    Apple), IN_APP_PURCHASE, HEALTHKIT, NFC_TAG_READING, APP_GROUPS, ICLOUD. Recreate profiles afterwards
    (signing_quick_setup) so they carry the new entitlement."""
    a = asc_mod.ASC()
    b = a.find_bundle_id(identifier)
    if not b:
        raise BridgeError(f"Bundle id {identifier} not registered (asc_register_bundle_id).")
    if capability in a.bundle_capabilities(b["id"]):
        return {"capability": capability, "already_enabled": True}
    a.enable_capability(b["id"], capability)
    return {"capability": capability, "enabled": True}


@mcp.tool()
@threaded
def asc_register_device(udid: P = None, name: P = None) -> dict:
    """Register an iPhone for ad-hoc/development builds. With no udid, uses the USB-connected device
    (needs pymobiledevice3)."""
    if not udid:
        devs = device.list_devices()
        if len(devs) != 1:
            raise BridgeError(f"Pass udid explicitly; found {len(devs)} connected devices: {devs}")
        udid, name = devs[0]["udid"], name or devs[0].get("name")
    a = asc_mod.ASC()
    for d in a.devices():
        if d["attributes"]["udid"].lower() == udid.lower():
            return {"id": d["id"], "udid": udid, "already_registered": True}
    d = a.register_device(name or f"iPhone {udid[:6]}", udid)
    return {"id": d["id"], "udid": udid, "registered": True,
            "next": "Re-run signing_quick_setup(profile_kind='ad-hoc') so the profile includes this device."}


@mcp.tool()
@threaded
def asc_revoke_certificate(certificate_id: str) -> dict:
    """Revoke a signing certificate (frees a slot when Apple's certificate limit is hit). Profiles using it become
    invalid; apps already on the App Store are unaffected."""
    asc_mod.ASC().revoke_certificate(certificate_id)
    return {"revoked": certificate_id}


@mcp.tool()
@threaded
def asc_download_profile(profile_id: str, dest_path: P = None) -> dict:
    """Download a provisioning profile (.mobileprovision) by id."""
    import base64
    from pathlib import Path
    prof = asc_mod.ASC().profile(profile_id)
    data = base64.b64decode(prof["attributes"]["profileContent"])
    dest = Path(dest_path).expanduser() if dest_path else signing.signing_dir() / "profiles" / f"{profile_id}.mobileprovision"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {"path": str(dest), **signing.profile_summary(data)}


@mcp.tool()
@threaded
def testflight_builds(bundle_id: P = None, limit: int = 10, project_path: P = None) -> dict:
    """List recent TestFlight/App Store builds for the app with their processing state."""
    bundle_id = bundle_id or project.main_bundle_id(project.resolve_project(project_path))
    a = asc_mod.ASC()
    app = a.app_by_bundle(bundle_id)
    return {"app": {"id": app["id"], "name": app["attributes"].get("name")}, "builds": a.builds(app["id"], limit),
            "beta_groups": asc_mod._short(a.beta_groups(app["id"]), ["name", "isInternalGroup", "publicLinkEnabled"])}


@mcp.tool()
@threaded
def testflight_distribute(build_id: str, beta_group_ids: Optional[list[str]] = None,
                          uses_non_exempt_encryption: Optional[bool] = False) -> dict:
    """Answer export compliance for a processed build (uses_non_exempt_encryption; None = leave as is) and add it to
    TestFlight beta groups (ids from testflight_builds)."""
    a = asc_mod.ASC()
    done = []
    if uses_non_exempt_encryption is not None:
        a.set_build_encryption(build_id, uses_non_exempt_encryption)
        done.append(f"usesNonExemptEncryption={uses_non_exempt_encryption}")
    for g in beta_group_ids or []:
        a.add_build_to_group(g, build_id)
        done.append(f"added to group {g}")
    return {"build_id": build_id, "done": done}


# ====================================================================== remote Mac

@mcp.tool()
@threaded
def mac_doctor() -> dict:
    """Check the remote Mac: macOS, Xcode, Flutter, CocoaPods and installed simulator runtimes."""
    return remote_mac.doctor()


@mcp.tool()
@threaded
def mac_sync_project(project_path: P = None, clean: bool = False) -> dict:
    """Upload the Flutter project to the remote Mac (skips build/, .dart_tool/, Pods/, .git). clean=true wipes the
    remote copy first."""
    return remote_mac.sync(project_path, clean)


@mcp.tool()
@threaded
def mac_build(mode: Literal["simulator", "no-codesign", "ipa"] = "simulator", sync_first: bool = True,
              extra_args: str = "", project_path: P = None) -> dict:
    """Build the project on the remote Mac (syncs first by default). Returns ok + the tail of the build output."""
    return remote_mac.build(project_path, mode, sync_first, extra_args)


@mcp.tool()
@threaded
def mac_list_simulators(only_booted: bool = False) -> list[dict]:
    """List iOS simulators on the remote Mac."""
    return remote_mac.list_simulators(only_booted)


@mcp.tool()
@threaded
def mac_boot_simulator(device_name_or_udid: P = None) -> dict:
    """Boot a simulator (default: an already-booted one or the newest iPhone)."""
    return remote_mac.boot(device_name_or_udid)


@mcp.tool()
@threaded
def mac_run_on_simulator(project_path: P = None, device_name_or_udid: P = None, sync_first: bool = True,
                         rebuild: bool = True) -> dict:
    """Sync, build for the simulator, install and launch the app on the remote Mac's iOS Simulator."""
    return remote_mac.run_on_simulator(project_path, device_name_or_udid, sync_first, rebuild)


@mcp.tool()
@threaded
def mac_install_simulator_app(app_zip_path: str, bundle_id: P = None, device_name_or_udid: P = None) -> dict:
    """Install a zipped simulator .app (e.g. from ci_download_artifacts) on the remote Mac's simulator and launch it."""
    return remote_mac.install_app_zip(app_zip_path, device_name_or_udid, bundle_id)


@mcp.tool()
@threaded
def mac_screenshot(device_name_or_udid: P = None) -> Image:
    """Take a screenshot of the iOS Simulator on the remote Mac and return it as an image."""
    return Image(data=remote_mac.screenshot(device_name_or_udid), format="png")


@mcp.tool()
@threaded
def mac_simulator_logs(seconds: int = 60, process: str = "Runner", grep: P = None,
                       device_name_or_udid: P = None) -> dict:
    """Recent unified-log lines from the app running in the simulator (Flutter print/debugPrint show up here)."""
    return remote_mac.simulator_logs(device_name_or_udid, seconds, process, grep)


@mcp.tool()
@threaded
def mac_open_url(url: str, device_name_or_udid: P = None) -> dict:
    """Open a URL / deep link / universal link in the simulator."""
    return remote_mac.open_url(url, device_name_or_udid)


@mcp.tool()
@threaded
def mac_exec(command: str, in_project: bool = True, project_path: P = None, timeout_seconds: int = 600) -> dict:
    """Run a shell command on the remote Mac (login shell). in_project=true runs it inside the synced project dir,
    e.g. `cd ios && pod install --repo-update` or `flutter test`."""
    return remote_mac.exec_command(command, in_project, project_path, timeout_seconds)


@mcp.tool()
@threaded
def mac_fetch_file(remote_path: str, local_dest: str, project_path: P = None) -> dict:
    """Download a file from the remote Mac (relative paths resolve inside the synced project), e.g.
    build/ios/ipa/app.ipa."""
    return remote_mac.fetch(remote_path, local_dest, project_path)


# ====================================================================== USB device

@mcp.tool()
@threaded
def device_list() -> list[dict]:
    """List iPhones/iPads connected over USB/Wi-Fi to this Windows/Linux machine."""
    return device.list_devices()


@mcp.tool()
@threaded
def device_info(udid: P = None) -> dict:
    """Model, iOS version and UDID of a connected device."""
    return device.info(udid)


@mcp.tool()
@threaded
def device_install_app(ipa_path: str, udid: P = None) -> dict:
    """Install a signed .ipa (ad-hoc or development profile that includes this device) on a connected iPhone."""
    return device.install(ipa_path, udid)


@mcp.tool()
@threaded
def device_list_apps(udid: P = None) -> list[dict]:
    """List user-installed apps on the connected device."""
    return device.list_apps(udid)


@mcp.tool()
@threaded
def device_uninstall_app(bundle_id: str, udid: P = None) -> dict:
    """Uninstall an app from the connected device."""
    return device.uninstall(bundle_id, udid)


@mcp.tool()
@threaded
def device_syslog(seconds: int = 15, match: P = None, process: P = "Runner", udid: P = None) -> dict:
    """Capture the device syslog for N seconds (max 120), optionally filtered by text or process name. Launch the app
    on the phone while this runs to catch crash reasons."""
    return device.syslog(seconds, match, process, udid)


# ====================================================================== prompts

@mcp.prompt()
def ship_to_testflight(project_path: str = "") -> str:
    """Guided flow: from a Windows/Linux Flutter project to a TestFlight build."""
    where = f" for the project at {project_path}" if project_path else ""
    return (f"Get this Flutter app onto TestFlight{where} without a Mac. Steps: call bridge_status; run ios_doctor and "
            "fix every error (ask me for permission strings wording and the final bundle id); ci_generate_workflow; "
            "ask me to commit and push; ci_trigger_build unsigned-ipa to confirm it compiles; if it fails use ci_get_logs "
            "and fix; make sure the App Store Connect app record exists (testflight_builds tells you); "
            "signing_quick_setup(profile_kind='app-store'); ci_trigger_build(build_type='signed-ipa', testflight=True); "
            "ci_wait_for_run until done; then testflight_builds and testflight_distribute once processing finishes.")


@mcp.prompt()
def fix_ios_build(run_id: str) -> str:
    """Diagnose and fix a failed iOS CI build."""
    return (f"The iOS build run {run_id} failed. Call ci_get_logs({run_id}), identify the root cause (CocoaPods / "
            "deployment target / signing / Swift compile / missing plist key), fix it in the project with the ios_* tools "
            "or by editing files, explain the fix, and tell me what to commit before re-triggering.")
