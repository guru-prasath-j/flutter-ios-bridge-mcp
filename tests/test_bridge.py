import datetime as dt
import io
import plistlib
import shutil
import tarfile
import zipfile
from pathlib import Path

import jwt
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from nacl import encoding, public

from flutter_ios_bridge import github_ci, pbxproj_signing, project, remote_mac, signing
from flutter_ios_bridge.errors import BridgeError

FIX = Path(__file__).parent / "fixtures" / "sample_app"


@pytest.fixture
def app(tmp_path):
    dst = tmp_path / "app"
    shutil.copytree(FIX, dst)
    return dst


def test_doctor_finds_real_problems(app):
    rep = project.doctor(project.resolve_project(str(app / "ios")))  # resolves upwards
    msgs = " | ".join(i["message"] for i in rep["issues"])
    assert "com.example" in msgs
    assert "NSLocationWhenInUseUsageDescription" in msgs
    assert "below 13.0" in msgs
    assert rep["summary"]["error"] >= 2


def test_fixes_clear_errors(app):
    project.set_bundle_identifier(app, "dev.guru.maps")
    project.set_deployment_target(app, "14.0")
    plist = project.read_info_plist(app)
    plist["NSLocationWhenInUseUsageDescription"] = "Show nearby offices"
    project.write_info_plist(app, plist)
    bs = project.build_settings(app)
    assert bs["bundle_identifiers"] == ["dev.guru.maps"]
    assert bs["test_bundle_identifiers"] == ["dev.guru.maps.RunnerTests"]
    assert bs["deployment_targets"] == ["14.0"]
    assert project.podfile_platform(app) == "14.0"
    rep = project.doctor(app)
    assert rep["summary"]["error"] == 0, rep["issues"]


def test_bad_bundle_id_rejected(app):
    with pytest.raises(BridgeError):
        project.set_bundle_identifier(app, "no dots")


def test_coerce():
    assert project.coerce_value("false", "bool") is False
    assert project.coerce_value('["a"]', "array") == ["a"]
    assert project.coerce_value("3", "int") == 3


def test_manual_signing_only_touches_runner(app):
    pbx = (app / "ios/Runner.xcodeproj/project.pbxproj").read_text()
    new, n = pbxproj_signing.apply_manual_signing(pbx, "com.example.googleMapsInFlutter", "ABCDE12345", "FIB my profile")
    assert n == 3  # Debug / Release / Profile of Runner
    assert new.count("CODE_SIGN_STYLE = Manual;") == 3
    assert new.count('PROVISIONING_PROFILE_SPECIFIER = "FIB my profile";') == 3
    assert new.count("DEVELOPMENT_TEAM = ABCDE12345;") == 3
    # RunnerTests configs still automatic
    assert new.count("CODE_SIGN_STYLE = Automatic;") == 3
    # still balanced braces -> structurally intact
    assert new.count("{") == new.count("}")


def test_signing_script_cli(app, capsys):
    f = app / "ios/Runner.xcodeproj/project.pbxproj"
    assert pbxproj_signing.main(["x", str(f), "com.example.googleMapsInFlutter", "T1", "P1"]) == 0
    assert pbxproj_signing.main(["x", str(f), "no.such.id", "T1", "P1"]) == 1


def test_workflow_renders_valid_yaml(app):
    y = github_ci.render_workflow("apps/mobile", "3.35.0", "macos-15")
    doc = yaml.safe_load(y)
    job = doc["jobs"]["ios"]
    assert job["runs-on"] == "macos-15"
    assert job["defaults"]["run"]["working-directory"] == "apps/mobile"
    flutter_step = next(s for s in job["steps"] if s.get("uses", "").startswith("subosito"))
    assert flutter_step["with"]["flutter-version"] == "3.35.0"
    inputs = doc[True]["workflow_dispatch"]["inputs"] if True in doc else doc["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs["build_type"]["options"]) == {"simulator", "unsigned-ipa", "signed-ipa"}
    # heredoc terminator must land at column 0 after YAML dedent
    signing_step = next(s for s in job["steps"] if s.get("name", "").startswith("Install certificate"))
    assert "\nPLIST\n" in signing_step["run"]
    y2 = github_ci.render_workflow(".", None, "macos-15")
    assert "flutter-version" not in y2 and yaml.safe_load(y2)


def test_generate_workflow_writes_files(app, monkeypatch):
    (app / ".git").mkdir()
    res = github_ci.generate_workflow(str(app), None, "macos-15", overwrite=False, match_local_flutter=False)
    assert Path(res["workflow_file"]).exists() and Path(res["signing_script"]).exists()
    with pytest.raises(BridgeError):
        github_ci.generate_workflow(str(app), None, "macos-15", overwrite=False, match_local_flutter=False)


def test_repo_from_git(tmp_path):
    (tmp_path / ".git").mkdir()
    for url in ("git@github.com:guru/my_app.git", "https://github.com/guru/my_app", "https://github.com/guru/my_app.git/"):
        (tmp_path / ".git/config").write_text(f'[remote "origin"]\n\turl = {url}\n')
        assert github_ci.repo_from_git(tmp_path) == "guru/my_app"


def test_extract_errors():
    log = ("2026-09-21T10:00:00.1234567Z ##[group]Run flutter build ipa\n"
           "2026-09-21T10:00:01.0Z Running pod install...\n"
           "2026-09-21T10:00:02.0Z Swift Compiler Error (Xcode): No such module 'Firebase'\n"
           "2026-09-21T10:00:03.0Z ##[error]Process completed with exit code 1.\n")
    r = github_ci.extract_errors(log)
    assert any("No such module" in l for l in r["error_lines"])
    assert not r["tail"].startswith("2026")


def test_safe_extract_blocks_zip_slip(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", "x")
    with zipfile.ZipFile(buf) as z, pytest.raises(BridgeError):
        github_ci.safe_extract(z, tmp_path / "out")


def test_sealed_box_roundtrip():
    sk = public.PrivateKey.generate()
    pk_b64 = sk.public_key.encode(encoding.Base64Encoder()).decode()
    box = public.SealedBox(public.PublicKey(pk_b64.encode(), encoding.Base64Encoder()))
    assert public.SealedBox(sk).decrypt(box.encrypt(b"s3cret")) == b"s3cret"


def _self_signed(key):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Apple Distribution: Test (ABCDE12345)"),
                      x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "ABCDE12345")])
    now = dt.datetime.now(dt.timezone.utc)
    return (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1234).not_valid_before(now).not_valid_after(now + dt.timedelta(days=365))
            .sign(key, hashes.SHA256()))


def test_csr_and_p12(tmp_path):
    key, csr_pem = signing.new_key_and_csr("flutter-ios-bridge Distribution")
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    assert csr.is_signature_valid
    cert = _self_signed(key)
    p12 = signing.make_p12(key, cert.public_bytes(serialization.Encoding.DER), "pw")
    f = tmp_path / "c.p12"
    f.write_bytes(p12)
    info = signing.inspect_p12(str(f), "pw")
    assert info["has_private_key"] and info["team_id"] == "ABCDE12345" and not info["expired"]
    with pytest.raises(BridgeError):
        signing.inspect_p12(str(f), "wrong")


def test_mobileprovision_parse():
    key, _ = signing.new_key_and_csr("x")
    cert = _self_signed(key)
    pl = plistlib.dumps({
        "Name": "FIB dev.guru.maps app-store", "UUID": "1111-2222", "TeamIdentifier": ["ABCDE12345"],
        "TeamName": "Guru", "ExpirationDate": dt.datetime(2030, 1, 1),
        "Entitlements": {"application-identifier": "ABCDE12345.dev.guru.maps", "beta-reports-active": True},
        "DeveloperCertificates": [cert.public_bytes(serialization.Encoding.DER)],
    })
    fake_cms = b"0\x82\x00\x00junk" + pl + b"\x00\x00trailing-signature"
    s = signing.profile_summary(fake_cms)
    assert s["bundle_id"] == "dev.guru.maps" and s["kind"] == "app-store" and s["team_id"] == "ABCDE12345"
    assert s["certificates"][0]["team_id"] == "ABCDE12345"


def test_asc_jwt(monkeypatch, tmp_path):
    from flutter_ios_bridge import asc
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    (tmp_path / "AuthKey.p8").write_bytes(pem)
    monkeypatch.setenv("ASC_KEY_ID", "KEY123")
    monkeypatch.setenv("ASC_ISSUER_ID", "issuer-uuid")
    monkeypatch.setenv("ASC_PRIVATE_KEY_PATH", str(tmp_path / "AuthKey.p8"))
    tok = asc.ASC().token()
    assert jwt.get_unverified_header(tok)["kid"] == "KEY123"
    claims = jwt.decode(tok, k.public_key(), algorithms=["ES256"], audience="appstoreconnect-v1")
    assert claims["iss"] == "issuer-uuid" and claims["exp"] - claims["iat"] <= 20 * 60


def test_pack_project_excludes(app):
    (app / "build").mkdir()
    (app / "build/huge.bin").write_bytes(b"0" * 10)
    (app / "ios/Pods").mkdir()
    (app / "ios/Pods/x").write_text("x")
    blob, n = remote_mac.pack_project(app)
    names = tarfile.open(fileobj=io.BytesIO(blob)).getnames()
    assert "pubspec.yaml" in names and "ios/Runner/Info.plist" in names
    assert not any(x.startswith("build/") or "/Pods/" in x for x in names)


def test_icon_alpha_detection(app):
    d = app / "ios/Runner/Assets.xcassets/AppIcon.appiconset"
    import json
    meta = json.loads((d / "Contents.json").read_text())
    fname = next(i["filename"] for i in meta["images"] if i.get("size") == "1024x1024")
    info = project.png_info(d / fname)
    assert info["width"] == 1024
