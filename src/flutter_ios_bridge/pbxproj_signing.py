#!/usr/bin/env python3
"""Switch the Runner target of a Flutter iOS project to manual code signing.

This file has no third-party dependencies on purpose: flutter-ios-bridge copies it
into your repo (.github/scripts/ios_manual_signing.py) so the macOS CI runner can
run it before `flutter build ipa`. Only build configurations whose
PRODUCT_BUNDLE_IDENTIFIER equals the app bundle id are touched, so CocoaPods
targets and RunnerTests keep their own settings.

usage: ios_manual_signing.py <project.pbxproj> <bundle_id> <team_id> <profile_name> [identity]
"""

import re
import sys

SIGNING_KEYS = (
    "CODE_SIGN_STYLE",
    "DEVELOPMENT_TEAM",
    "PROVISIONING_PROFILE_SPECIFIER",
    "PROVISIONING_PROFILE",
    "CODE_SIGN_IDENTITY",
    '"CODE_SIGN_IDENTITY[sdk=iphoneos*]"',
)

_BLOCK_RE = re.compile(r"(buildSettings = \{\n)(.*?)(\n(\t*)\};)", re.S)


def _quote(v: str) -> str:
    return v if re.fullmatch(r"[A-Za-z0-9_.$/]+", v) else '"' + v.replace('"', '\\"') + '"'


def apply_manual_signing(text: str, bundle_id: str, team_id: str, profile_name: str,
                         identity: str = "Apple Distribution") -> tuple[str, int]:
    bid_re = re.compile(r"^\s*PRODUCT_BUNDLE_IDENTIFIER = \"?" + re.escape(bundle_id) + r"\"?;\s*$", re.M)
    count = 0

    def fix(m: re.Match) -> str:
        nonlocal count
        head, body, tail, indent = m.group(1), m.group(2), m.group(3), m.group(4)
        if not bid_re.search(body):
            return m.group(0)
        count += 1
        line_indent = indent + "\t"
        kept = [ln for ln in body.split("\n")
                if not any(ln.strip().startswith(k + " =") for k in SIGNING_KEYS)]
        extra = [
            "CODE_SIGN_STYLE = Manual;",
            f"DEVELOPMENT_TEAM = {_quote(team_id)};",
            f"PROVISIONING_PROFILE_SPECIFIER = {_quote(profile_name)};",
            f"CODE_SIGN_IDENTITY = {_quote(identity)};",
            f'"CODE_SIGN_IDENTITY[sdk=iphoneos*]" = {_quote(identity)};',
        ]
        return head + "\n".join(kept + [line_indent + e for e in extra]) + tail

    return _BLOCK_RE.sub(fix, text), count


def main(argv: list[str]) -> int:
    if len(argv) < 5:
        print(__doc__)
        return 2
    path, bundle_id, team_id, profile = argv[1:5]
    identity = argv[5] if len(argv) > 5 else "Apple Distribution"
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    new, n = apply_manual_signing(text, bundle_id, team_id, profile, identity)
    if n == 0:
        print(f"error: no build configuration with PRODUCT_BUNDLE_IDENTIFIER = {bundle_id}", file=sys.stderr)
        return 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    print(f"manual signing applied to {n} build configuration(s) for {bundle_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
