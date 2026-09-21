"""Backend flows against mocked GitHub / App Store Connect / Mac, so CI needs no credentials."""

import base64
import io
import json
import zipfile

import httpx
import pytest
from nacl import encoding, public

from flutter_ios_bridge import asc as asc_mod, github_ci, remote_mac


class FakeGitHub:
    def __init__(self):
        self.sk = public.PrivateKey.generate()
        self.secrets: dict[str, str] = {}
        self.dispatched = None
        self.run_polls = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        if p == "/repos/guru/app" and m == "GET":
            return httpx.Response(200, json={"default_branch": "main"})
        if p.endswith("/actions/secrets/public-key"):
            return httpx.Response(200, json={"key_id": "k1", "key": self.sk.public_key.encode(encoding.Base64Encoder()).decode()})
        if p.startswith("/repos/guru/app/actions/secrets/") and m == "PUT":
            body = json.loads(req.content)
            name = p.rsplit("/", 1)[1]
            self.secrets[name] = public.SealedBox(self.sk).decrypt(base64.b64decode(body["encrypted_value"])).decode()
            return httpx.Response(201)
        if p == "/repos/guru/app/actions/secrets":
            return httpx.Response(200, json={"secrets": [{"name": n} for n in self.secrets]})
        if p.endswith("/dispatches"):
            self.dispatched = json.loads(req.content)
            return httpx.Response(204)
        if p.endswith("/flutter-ios-bridge.yml/runs"):
            rid = self.dispatched["inputs"]["request_id"] if self.dispatched else "zzz"
            return httpx.Response(200, json={"workflow_runs": [
                {"id": 42, "display_title": f"iOS unsigned-ipa {rid}", "status": "queued", "conclusion": None,
                 "head_branch": "main", "head_sha": "abcdef123", "created_at": "now", "html_url": "https://x"}]})
        if p == "/repos/guru/app/actions/runs/42":
            self.run_polls += 1
            done = self.run_polls > 1
            return httpx.Response(200, json={"id": 42, "status": "completed" if done else "in_progress",
                                             "conclusion": "failure" if done else None, "display_title": "t"})
        if p == "/repos/guru/app/actions/runs/42/jobs":
            return httpx.Response(200, json={"jobs": [{"id": 7, "name": "ios", "status": "completed", "conclusion": "failure",
                                                       "steps": [{"name": "Build unsigned IPA", "status": "completed", "conclusion": "failure"}]}]})
        if p == "/repos/guru/app/actions/jobs/7/logs":
            return httpx.Response(200, text="2026-01-01T00:00:00Z Error (Xcode): Undefined symbol: _foo\n2026-01-01T00:00:00Z done\n")
        if p == "/repos/guru/app/actions/runs/42/artifacts":
            return httpx.Response(200, json={"artifacts": [{"name": "ios-unsigned-ipa", "expired": False,
                                                            "archive_download_url": "https://api.github.com/dl/1"}]})
        if p == "/dl/1":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("Runner-unsigned.ipa", b"PK fake ipa")
            return httpx.Response(200, content=buf.getvalue())
        return httpx.Response(404, json={"message": f"unmocked {m} {p}"})


@pytest.fixture
def gh(monkeypatch, tmp_path):
    fake = FakeGitHub()
    real = httpx.Client
    monkeypatch.setattr(github_ci.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(fake.handler), **kw))
    monkeypatch.setattr(github_ci.time, "sleep", lambda s: None)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("FIB_GITHUB_REPO", "guru/app")
    monkeypatch.setenv("FIB_CONFIG", str(tmp_path / "none.toml"))
    return fake


def test_trigger_wait_logs_download(gh, tmp_path, monkeypatch):
    import shutil
    from pathlib import Path
    app = tmp_path / "app"
    shutil.copytree(Path(__file__).parent / "fixtures/sample_app", app)
    res = github_ci.trigger(str(app), "unsigned-ipa", "app-store-connect", False, None, "", "", None)
    assert res["ref"] == "main" and res["run"]["id"] == 42
    assert gh.dispatched["inputs"]["build_type"] == "unsigned-ipa"
    w = github_ci.wait_for_run(42, 60, 5, str(app), None)
    assert w["run"]["conclusion"] == "failure" and "ci_get_logs" in w["note"]
    lg = github_ci.logs(42, True, 20, str(app), None)
    assert lg["jobs"][0]["failed_steps"] == ["Build unsigned IPA"]
    assert any("Undefined symbol" in l for l in lg["jobs"][0]["error_lines"])
    dl = github_ci.download_artifacts(42, str(tmp_path / "out"), str(app), None)
    assert dl["files"][0]["path"].endswith("Runner-unsigned.ipa") and dl["hints"]


def test_signed_build_requires_secrets(gh, tmp_path):
    import shutil
    from pathlib import Path
    from flutter_ios_bridge.errors import BridgeError
    app = tmp_path / "app"
    shutil.copytree(Path(__file__).parent / "fixtures/sample_app", app)
    with pytest.raises(BridgeError, match="signing_quick_setup"):
        github_ci.trigger(str(app), "signed-ipa", "app-store-connect", True, None, "", "", None)


def test_set_secret_encrypts(gh):
    g = github_ci.GitHub(repo="guru/app")
    g.set_secret("IOS_P12_PASSWORD", "hunter2")
    assert gh.secrets == {"IOS_P12_PASSWORD": "hunter2"}


# ------------------------------------------------------------------ App Store Connect quick setup

def test_quick_setup_flow(monkeypatch, tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import datetime as dt
    import plistlib

    k = ec.generate_private_key(ec.SECP256R1())
    (tmp_path / "k.p8").write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
    monkeypatch.setenv("ASC_KEY_ID", "K"); monkeypatch.setenv("ASC_ISSUER_ID", "I")
    monkeypatch.setenv("ASC_PRIVATE_KEY_PATH", str(tmp_path / "k.p8"))
    monkeypatch.setenv("FIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FIB_CONFIG", str(tmp_path / "none.toml"))
    state = {"certs": [], "profiles": []}

    def handler(req: httpx.Request):
        p, m = req.url.path, req.method
        assert req.headers["Authorization"].startswith("Bearer ")
        if p == "/v1/bundleIds" and m == "GET":
            return httpx.Response(200, json={"data": []})
        if p == "/v1/bundleIds" and m == "POST":
            return httpx.Response(201, json={"data": {"id": "B1", "attributes": json.loads(req.content)["data"]["attributes"]}})
        if p == "/v1/certificates" and m == "GET":
            return httpx.Response(200, json={"data": state["certs"]})
        if p == "/v1/certificates" and m == "POST":
            csr = x509.load_pem_x509_csr(json.loads(req.content)["data"]["attributes"]["csrContent"].encode())
            ca = ec.generate_private_key(ec.SECP256R1())
            now = dt.datetime.now(dt.timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(csr.subject)
                    .public_key(csr.public_key()).serial_number(9).not_valid_before(now)
                    .not_valid_after(now + dt.timedelta(days=365)).sign(ca, hashes.SHA256()))
            c = {"id": "C1", "attributes": {"certificateContent": base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode(),
                                            "displayName": "Dist", "name": "Dist"}}
            state["certs"].append(c)
            return httpx.Response(201, json={"data": c})
        if p == "/v1/profiles" and m == "GET":
            return httpx.Response(200, json={"data": state["profiles"]})
        if p == "/v1/profiles" and m == "POST":
            body = json.loads(req.content)["data"]
            assert body["relationships"]["certificates"]["data"][0]["id"] == "C1"
            pl = plistlib.dumps({"Name": body["attributes"]["name"], "UUID": "U", "TeamIdentifier": ["T"],
                                 "Entitlements": {"application-identifier": "T.dev.guru.maps"},
                                 "ExpirationDate": dt.datetime(2030, 1, 1)})
            prof = {"id": "P1", "attributes": {"profileContent": base64.b64encode(b"cms" + pl).decode()}}
            state["profiles"].append(prof)
            return httpx.Response(201, json={"data": prof})
        if p.startswith("/v1/profiles/") and m == "DELETE":
            state["profiles"].clear()
            return httpx.Response(204)
        return httpx.Response(404, json={"errors": [{"title": "unmocked", "detail": f"{m} {p}"}]})

    real = httpx.Client
    monkeypatch.setattr(asc_mod.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    r1 = asc_mod.quick_setup("dev.guru.maps", "app-store", None, "pw", False)
    assert r1["certificate_id"] == "C1" and r1["profile"]["bundle_id"] == "dev.guru.maps"
    from flutter_ios_bridge import signing
    assert signing.inspect_p12(r1["p12_path"], "pw")["has_private_key"]
    # second run reuses the certificate (private key kept locally) and replaces the profile
    r2 = asc_mod.quick_setup("dev.guru.maps", "app-store", None, "pw2", False)
    assert r2["certificate_id"] == "C1" and any("Reusing" in s for s in r2["steps"])
    assert any("Deleted old profile" in s for s in r2["steps"])


# ------------------------------------------------------------------ Mac simulator selection

class FakeMac:
    SIMS = {"devices": {
        "com.apple.CoreSimulator.SimRuntime.iOS-18-2": [
            {"name": "iPhone 16", "udid": "A", "state": "Shutdown"},
            {"name": "iPhone 16 Pro", "udid": "B", "state": "Shutdown"}],
        "com.apple.CoreSimulator.SimRuntime.watchOS-11-0": [{"name": "Watch", "udid": "W", "state": "Booted"}],
    }}

    def run(self, cmd, **kw):
        return {"exit_code": 0, "stdout": json.dumps(self.SIMS), "stderr": ""}


def test_pick_simulator():
    mac = FakeMac()
    assert remote_mac._pick_sim(mac, None) == "B"  # newest iPhone, ignores booted watch
    assert remote_mac._pick_sim(mac, "iPhone 16") == "A"
    FakeMac.SIMS["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-18-2"][0]["state"] = "Booted"
    assert remote_mac._pick_sim(mac, None) == "A"
