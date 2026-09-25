# flutter-ios-bridge-mcp

[![tests](https://github.com/guru-prasath-j/flutter-ios-bridge-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/guru-prasath-j/flutter-ios-bridge-mcp/actions/workflows/tests.yml)
[![M8ven Score](https://m8ven.ai/badge/mcp/guru-prasath-j/flutter-ios-bridge-mcp)](https://m8ven.ai/mcp/guru-prasath-j/flutter-ios-bridge-mcp)

**Ship Flutter apps to iOS from Windows or Linux — no Mac needed.**

An [MCP](https://modelcontextprotocol.io) server that gives your AI assistant (Claude Desktop, Claude Code, Cursor, VS Code Copilot, Windsurf…) the tools to build, sign, test and upload iOS apps. It connects your machine to the Apple parts of the toolchain:

| Backend | What it gives you | Needs |
|---|---|---|
| **Project tools** (always on) | `ios_doctor` finds iOS-only problems before you burn CI minutes; `ios_*` tools fix Info.plist, bundle id, deployment target, Podfile — no Xcode | nothing |
| **GitHub Actions macOS runners** | Simulator `.app`, unsigned IPA (for sideloading), signed IPA, TestFlight upload; pulls errors and artifacts back | GitHub repo + token |
| **App Store Connect API** | Certificates from a locally generated CSR (no Keychain), provisioning profiles, bundle ids, capabilities, devices, TestFlight builds & groups | Apple Developer account + API key |
| **Remote Mac over SSH** (optional) | Sync → build → run on the iOS Simulator → **screenshots returned to the AI** → logs, deep links | any Mac you can SSH into |
| **USB iPhone** (optional) | List devices, install IPAs, read syslog from Windows/Linux | `pymobiledevice3` + Apple USB driver |

Read the story behind it: [No Mac? You can still ship your Flutter app to iOS](https://medium.com/@guruprasathjay/no-mac-you-can-still-ship-your-flutter-app-to-ios-i-built-an-mcp-server-for-it-86fb85fa971f). A full working example: [flutter-ios-bridge-demo](https://github.com/guru-prasath-j/flutter-ios-bridge-demo).

## Install

```bash
pipx install flutter-ios-bridge-mcp                                              # from PyPI
pipx install git+https://github.com/guru-prasath-j/flutter-ios-bridge-mcp.git   # latest from GitHub

# optional: USB iPhone support
pipx inject flutter-ios-bridge-mcp pymobiledevice3
```

Then:

```bash
flutter-ios-bridge-mcp init       # writes a commented config.toml (or use env vars)
flutter-ios-bridge-mcp status     # shows what is configured
flutter-ios-bridge-mcp doctor path/to/flutter_app   # iOS audit, no AI needed
```

## Connect it to your MCP client

**Claude Desktop** (`%APPDATA%\Claude\claude_desktop_config.json` on Windows, `~/.config/Claude/…` on Linux):

```json
{
  "mcpServers": {
    "flutter-ios-bridge": {
      "command": "flutter-ios-bridge-mcp",
      "env": {
        "FIB_PROJECT_PATH": "C:/Users/me/code/my_app",
        "GITHUB_TOKEN": "github_pat_...",
        "ASC_KEY_ID": "ABC123XYZ",
        "ASC_ISSUER_ID": "00000000-0000-0000-0000-000000000000",
        "ASC_PRIVATE_KEY_PATH": "C:/keys/AuthKey_ABC123XYZ.p8"
      }
    }
  }
}
```

**Claude Code**: `claude mcp add flutter-ios-bridge -- flutter-ios-bridge-mcp`

**VS Code / Cursor** (`.vscode/mcp.json` / `.cursor/mcp.json`): same `command` + `env` shape.

No pipx on PATH? Use `"command": "pipx", "args": ["run", "flutter-ios-bridge-mcp"]`.

## Configuration

Environment variables win over `config.toml` (`%APPDATA%\flutter-ios-bridge\config.toml` on Windows, `~/.config/flutter-ios-bridge/config.toml` elsewhere; override with `FIB_CONFIG`).

| Variable | Purpose |
|---|---|
| `FIB_PROJECT_PATH` | Default Flutter project |
| `GITHUB_TOKEN` | Fine-grained PAT: *Actions* R/W, *Secrets* R/W, *Contents* R. Falls back to `gh auth token` |
| `FIB_GITHUB_REPO` | `owner/repo` (default: read from the project's git remote) |
| `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_PRIVATE_KEY_PATH` | App Store Connect API key |
| `FIB_MAC_HOST`, `FIB_MAC_USER`, `FIB_MAC_KEY_PATH` / `FIB_MAC_PASSWORD`, `FIB_MAC_PORT`, `FIB_MAC_REMOTE_DIR` | Optional remote Mac |

Signing material created by the tool lives in `~/.flutter-ios-bridge/signing/` (private keys are `chmod 600`). Back it up — the certificate private key never leaves your machine except as an encrypted GitHub secret.

## The zero-Mac path to TestFlight

Ask your assistant *"Ship my app to TestFlight"* (or use the built-in `ship_to_testflight` prompt). Under the hood:

1. `ios_doctor` → fix bundle id, missing permission strings, deployment target / Podfile mismatch, icon alpha channel.
2. `ci_generate_workflow` → commit & push `.github/`.
3. `ci_trigger_build(build_type="unsigned-ipa")` → `ci_wait_for_run` → on failure `ci_get_logs` pulls the exact Xcode/CocoaPods errors.
4. Create the app record once in App Store Connect.
5. `signing_quick_setup(profile_kind="app-store")` → GitHub secrets.
6. `ci_trigger_build(build_type="signed-ipa", testflight=true)`.
7. `testflight_builds` → `testflight_distribute`.

### Testing on a real iPhone without a Mac

* **Ad-hoc**: register the device → create signing material → build and install the IPA.
* **Free Apple ID**: build `unsigned-ipa` and sideload with Sideloadly / AltStore.
* **Simulator**: needs macOS; with a remote Mac use `mac_run_on_simulator` + `mac_screenshot`.

## Tools

| Group | Tools |
|---|---|
| status | `bridge_status` |
| project | `ios_doctor`, `ios_get_info_plist`, `ios_set_info_plist_value`, `ios_remove_info_plist_key`, `ios_get_build_settings`, `ios_set_bundle_identifier`, `ios_set_deployment_target` |
| CI | `ci_generate_workflow`, `ci_trigger_build`, `ci_list_runs`, `ci_wait_for_run`, `ci_get_logs`, `ci_download_artifacts`, `ci_cancel_run`, `ci_set_signing_secrets` |
| signing / ASC | `signing_quick_setup`, `signing_inspect`, `asc_list`, `asc_register_bundle_id`, `asc_enable_capability`, `asc_register_device`, `asc_revoke_certificate`, `asc_download_profile`, `testflight_builds`, `testflight_distribute` |
| remote Mac | `mac_doctor`, `mac_sync_project`, `mac_build`, `mac_list_simulators`, `mac_boot_simulator`, `mac_run_on_simulator`, `mac_install_simulator_app`, `mac_screenshot`, `mac_simulator_logs`, `mac_open_url`, `mac_exec`, `mac_fetch_file` |
| USB device | `device_list`, `device_info`, `device_install_app`, `device_list_apps`, `device_uninstall_app`, `device_syslog` |
| prompts | `ship_to_testflight`, `fix_ios_build` |

## Costs and limits

* GitHub-hosted macOS minutes are free on public repos; private repos consume your Actions minutes at a 10× rate. A clean Flutter iOS build takes roughly 8–20 minutes.
* Publishing to TestFlight/App Store requires a paid Apple Developer Program membership.
* Apple caps the number of distribution certificates; `signing_quick_setup` reuses the one it created, and `asc_revoke_certificate` frees a slot.
* Windows USB access needs Apple's driver; Linux needs `usbmuxd`.

## Security

* Tokens and keys are read from env/config only; nothing is sent anywhere except GitHub and Apple APIs (and your own Mac over SSH).
* GitHub secrets are encrypted locally with the repo's public key before upload.
* The CI workflow creates a throwaway keychain per run and deletes it afterwards.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]" && pytest
npx @modelcontextprotocol/inspector flutter-ios-bridge-mcp   # poke at the tools
```

MIT © Guruprasath J
