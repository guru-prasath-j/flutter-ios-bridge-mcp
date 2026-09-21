"""Minimal App Store Connect API client (JWT auth) — no Xcode, no fastlane, no Mac."""

import base64
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import jwt

from . import signing
from .config import Config, load_config
from .errors import BridgeError

API = "https://api.appstoreconnect.apple.com"

PROFILE_TYPES = {
    "app-store": "IOS_APP_STORE",
    "ad-hoc": "IOS_APP_ADHOC",
    "development": "IOS_APP_DEVELOPMENT",
}


class ASC:
    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or load_config()
        a = self.cfg.asc
        if not a.configured:
            raise BridgeError(
                "App Store Connect API key not configured. Create one at appstoreconnect.apple.com → Users and Access → "
                "Integrations → App Store Connect API (role: App Manager or Admin), then set ASC_KEY_ID, ASC_ISSUER_ID and "
                "ASC_PRIVATE_KEY_PATH (path to the AuthKey_XXXX.p8).")
        self.key_pem = a.private_key or Path(a.private_key_path).expanduser().read_text()
        self._token: Optional[str] = None
        self._exp = 0.0
        self.http = httpx.Client(base_url=API, timeout=60)

    def token(self) -> str:
        now = time.time()
        if not self._token or now > self._exp - 60:
            self._exp = now + 15 * 60
            self._token = jwt.encode(
                {"iss": self.cfg.asc.issuer_id, "iat": int(now), "exp": int(self._exp), "aud": "appstoreconnect-v1"},
                self.key_pem, algorithm="ES256", headers={"kid": self.cfg.asc.key_id, "typ": "JWT"},
            )
        return self._token

    def req(self, method: str, url: str, **kw) -> Any:
        r = self.http.request(method, url, headers={"Authorization": f"Bearer {self.token()}"}, **kw)
        if r.status_code >= 400:
            try:
                errs = r.json().get("errors", [])
                detail = "; ".join(f"{e.get('title')}: {e.get('detail')}" for e in errs) or r.text[:400]
            except Exception:
                detail = r.text[:400]
            raise BridgeError(f"App Store Connect {method} {url} -> {r.status_code}: {detail}")
        return r.json() if r.content else {}

    def get_all(self, url: str, params: Optional[dict] = None, cap: int = 1000) -> list[dict]:
        out: list[dict] = []
        data = self.req("GET", url, params={**(params or {}), "limit": 200})
        out += data.get("data", [])
        while data.get("links", {}).get("next") and len(out) < cap:
            data = self.req("GET", data["links"]["next"])
            out += data.get("data", [])
        return out

    # ------------------------------------------------ bundle ids
    def find_bundle_id(self, identifier: str) -> Optional[dict]:
        for b in self.get_all("/v1/bundleIds", {"filter[identifier]": identifier}):
            if b["attributes"]["identifier"] == identifier:
                return b
        return None

    def register_bundle_id(self, identifier: str, name: Optional[str] = None) -> dict:
        name = name or re.sub(r"[^A-Za-z0-9 ]", " ", identifier.split(".")[-1]).strip() or "App"
        return self.req("POST", "/v1/bundleIds", json={"data": {"type": "bundleIds", "attributes": {
            "identifier": identifier, "name": name, "platform": "IOS"}}})["data"]

    def enable_capability(self, bundle_resource_id: str, capability: str) -> dict:
        return self.req("POST", "/v1/bundleIdCapabilities", json={"data": {
            "type": "bundleIdCapabilities", "attributes": {"capabilityType": capability},
            "relationships": {"bundleId": {"data": {"type": "bundleIds", "id": bundle_resource_id}}}}})["data"]

    def bundle_capabilities(self, bundle_resource_id: str) -> list[str]:
        d = self.req("GET", f"/v1/bundleIds/{bundle_resource_id}/bundleIdCapabilities")
        return [c["attributes"]["capabilityType"] for c in d.get("data", [])]

    # ------------------------------------------------ devices
    def devices(self) -> list[dict]:
        return self.get_all("/v1/devices")

    def register_device(self, name: str, udid: str) -> dict:
        return self.req("POST", "/v1/devices", json={"data": {"type": "devices", "attributes": {
            "name": name, "udid": udid, "platform": "IOS"}}})["data"]

    # ------------------------------------------------ certificates
    def certificates(self, cert_type: Optional[str] = None) -> list[dict]:
        params = {"filter[certificateType]": cert_type} if cert_type else {}
        return self.get_all("/v1/certificates", params)

    def create_certificate(self, cert_type: str, csr_pem: str) -> dict:
        return self.req("POST", "/v1/certificates", json={"data": {"type": "certificates", "attributes": {
            "certificateType": cert_type, "csrContent": csr_pem}}})["data"]

    def revoke_certificate(self, cert_id: str) -> None:
        self.req("DELETE", f"/v1/certificates/{cert_id}")

    # ------------------------------------------------ profiles
    def profiles(self, name: Optional[str] = None) -> list[dict]:
        params = {"filter[name]": name} if name else {}
        return self.get_all("/v1/profiles", params)

    def profile(self, profile_id: str) -> dict:
        return self.req("GET", f"/v1/profiles/{profile_id}")["data"]

    def delete_profile(self, profile_id: str) -> None:
        self.req("DELETE", f"/v1/profiles/{profile_id}")

    def create_profile(self, name: str, profile_type: str, bundle_resource_id: str,
                       cert_ids: list[str], device_ids: list[str]) -> dict:
        rel: dict[str, Any] = {
            "bundleId": {"data": {"type": "bundleIds", "id": bundle_resource_id}},
            "certificates": {"data": [{"type": "certificates", "id": c} for c in cert_ids]},
        }
        if profile_type != "IOS_APP_STORE":
            rel["devices"] = {"data": [{"type": "devices", "id": d} for d in device_ids]}
        return self.req("POST", "/v1/profiles", json={"data": {"type": "profiles",
                        "attributes": {"name": name, "profileType": profile_type}, "relationships": rel}})["data"]

    # ------------------------------------------------ apps / testflight
    def app_by_bundle(self, bundle_id: str) -> dict:
        data = self.req("GET", "/v1/apps", params={"filter[bundleId]": bundle_id})["data"]
        if not data:
            raise BridgeError(f"No App Store Connect app record for {bundle_id}. Create it once in App Store Connect → Apps → + "
                              "(the API cannot create app records).")
        return data[0]

    def apps(self) -> list[dict]:
        return self.get_all("/v1/apps")

    def builds(self, app_id: str, limit: int = 10) -> list[dict]:
        d = self.req("GET", "/v1/builds", params={"filter[app]": app_id, "sort": "-uploadedDate",
                                                   "limit": limit, "include": "preReleaseVersion"})
        versions = {i["id"]: i["attributes"].get("version") for i in d.get("included", []) if i["type"] == "preReleaseVersions"}
        out = []
        for b in d.get("data", []):
            a = b["attributes"]
            pr = (b.get("relationships", {}).get("preReleaseVersion", {}).get("data") or {}).get("id")
            out.append({"id": b["id"], "version": versions.get(pr), "build_number": a.get("version"),
                        "uploaded": a.get("uploadedDate"), "processing_state": a.get("processingState"),
                        "expired": a.get("expired"), "uses_non_exempt_encryption": a.get("usesNonExemptEncryption")})
        return out

    def beta_groups(self, app_id: str) -> list[dict]:
        return self.req("GET", f"/v1/apps/{app_id}/betaGroups")["data"]

    def set_build_encryption(self, build_id: str, uses: bool) -> None:
        self.req("PATCH", f"/v1/builds/{build_id}", json={"data": {"type": "builds", "id": build_id,
                 "attributes": {"usesNonExemptEncryption": uses}}})

    def add_build_to_group(self, group_id: str, build_id: str) -> None:
        self.req("POST", f"/v1/betaGroups/{group_id}/relationships/builds",
                 json={"data": [{"type": "builds", "id": build_id}]})


# ======================================================== high level

def _short(items: list[dict], fields: list[str]) -> list[dict]:
    return [{"id": i["id"], **{f: i["attributes"].get(f) for f in fields}} for i in items]


def ensure_bundle(asc: ASC, bundle_id: str, name: Optional[str]) -> tuple[dict, bool]:
    b = asc.find_bundle_id(bundle_id)
    if b:
        return b, False
    return asc.register_bundle_id(bundle_id, name), True


def local_cert_key(cert_id: str) -> Optional[Path]:
    p = signing.signing_dir() / "certs" / f"{cert_id}.key.pem"
    return p if p.exists() else None


def create_certificate_files(asc: ASC, cert_type: str, p12_password: str, common_name: str) -> dict:
    key, csr = signing.new_key_and_csr(common_name)
    try:
        cert = asc.create_certificate(cert_type, csr)
    except BridgeError as e:
        if "limit" in str(e).lower() or "already have a current" in str(e).lower() or "409" in str(e):
            existing = _short(asc.certificates(cert_type), ["displayName", "expirationDate"])
            raise BridgeError(f"{e}\nApple limits how many {cert_type} certificates you can have. Existing: {existing}. "
                              "Revoke an unused one with asc_revoke_certificate (builds already on the App Store are not affected).")
        raise
    der = base64.b64decode(cert["attributes"]["certificateContent"])
    d = signing.signing_dir() / "certs"
    signing.write_secret(d / f"{cert['id']}.key.pem", signing.key_to_pem(key))
    (d / f"{cert['id']}.cer").write_bytes(der)
    p12 = signing.make_p12(key, der, p12_password, common_name)
    signing.write_secret(d / f"{cert['id']}.p12", p12)
    return {"id": cert["id"], "p12_path": str(d / f"{cert['id']}.p12"), "cer_path": str(d / f"{cert['id']}.cer"),
            "key_path": str(d / f"{cert['id']}.key.pem"), "expires": cert["attributes"].get("expirationDate")}


def p12_for_existing(asc_cert: dict, p12_password: str) -> Optional[bytes]:
    kp = local_cert_key(asc_cert["id"])
    if not kp:
        return None
    der = base64.b64decode(asc_cert["attributes"]["certificateContent"])
    return signing.make_p12(signing.load_key(kp.read_bytes()), der, p12_password, asc_cert["attributes"].get("name") or "cert")


def quick_setup(bundle_id: str, profile_kind: str, app_name: Optional[str], p12_password: Optional[str],
                force_new_certificate: bool) -> dict:
    if profile_kind not in PROFILE_TYPES:
        raise BridgeError(f"profile_kind must be one of {list(PROFILE_TYPES)}")
    asc = ASC()
    steps: list[str] = []
    bundle, created = ensure_bundle(asc, bundle_id, app_name)
    steps.append(("Registered" if created else "Found") + f" bundle id {bundle_id}")

    cert_type = "DEVELOPMENT" if profile_kind == "development" else "DISTRIBUTION"
    password = p12_password or secrets.token_urlsafe(18)
    p12: Optional[bytes] = None
    cert_id = None
    if not force_new_certificate:
        for c in asc.certificates(cert_type):
            p12 = p12_for_existing(c, password)
            if p12:
                cert_id = c["id"]
                steps.append(f"Reusing {cert_type} certificate {c['attributes'].get('displayName')} ({cert_id}) created earlier by this tool")
                break
    if not p12:
        info = create_certificate_files(asc, cert_type, password, f"flutter-ios-bridge {cert_type.title()}")
        cert_id = info["id"]
        p12 = Path(info["p12_path"]).read_bytes()
        steps.append(f"Created new {cert_type} certificate {cert_id} (private key kept in {info['key_path']})")

    ptype = PROFILE_TYPES[profile_kind]
    device_ids: list[str] = []
    if ptype != "IOS_APP_STORE":
        device_ids = [d["id"] for d in asc.devices() if d["attributes"].get("status") == "ENABLED"
                      and d["attributes"].get("platform") in ("IOS", "UNIVERSAL")]
        if not device_ids:
            raise BridgeError("ad-hoc/development profiles need at least one registered iPhone. "
                              "Plug it in and run device_list, then asc_register_device.")
    name = f"FIB {bundle_id} {profile_kind}"
    for old in asc.profiles(name):
        asc.delete_profile(old["id"])
        steps.append(f"Deleted old profile {old['id']} with the same name")
    prof = asc.create_profile(name, ptype, bundle["id"], [cert_id], device_ids)
    content = base64.b64decode(prof["attributes"]["profileContent"])
    out = signing.signing_dir() / bundle_id
    signing.write_secret(out / f"{profile_kind}.p12", p12)
    (out / f"{profile_kind}.mobileprovision").write_bytes(content)
    signing.write_secret(out / f"{profile_kind}.p12.password.txt", password.encode())
    steps.append(f"Created profile '{name}' ({len(device_ids)} devices)")
    return {
        "steps": steps,
        "bundle_resource_id": bundle["id"],
        "certificate_id": cert_id,
        "profile_id": prof["id"],
        "profile": signing.profile_summary(content),
        "p12_path": str(out / f"{profile_kind}.p12"),
        "p12_password": password,
        "mobileprovision_path": str(out / f"{profile_kind}.mobileprovision"),
    }
