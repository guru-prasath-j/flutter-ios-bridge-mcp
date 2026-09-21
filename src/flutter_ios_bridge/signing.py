"""Code-signing material without a Mac/Keychain: CSR, .p12 and .mobileprovision handling."""

import datetime as dt
import os
import plistlib
from pathlib import Path
from typing import Any, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from .config import data_dir
from .errors import BridgeError


def signing_dir() -> Path:
    d = data_dir() / "signing"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_secret(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def new_key_and_csr(common_name: str, email: Optional[str] = None) -> tuple[rsa.RSAPrivateKey, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, common_name), x509.NameAttribute(NameOID.COUNTRY_NAME, "US")]
    if email:
        attrs.insert(0, x509.NameAttribute(NameOID.EMAIL_ADDRESS, email))
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name(attrs)).sign(key, hashes.SHA256())
    return key, csr.public_bytes(serialization.Encoding.PEM).decode()


def key_to_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def load_key(pem: bytes):
    return serialization.load_pem_private_key(pem, password=None)


def make_p12(key, cert_der: bytes, password: str, name: str = "flutter-ios-bridge") -> bytes:
    cert = x509.load_der_x509_certificate(cert_der)
    # Legacy PBE (3DES + SHA1 MAC): the only flavour macOS `security import` reliably accepts.
    enc = (serialization.PrivateFormat.PKCS12.encryption_builder()
           .kdf_rounds(50000)
           .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
           .hmac_hash(hashes.SHA1())
           .build(password.encode()))
    return pkcs12.serialize_key_and_certificates(name.encode(), key, cert, None, enc)


def cert_summary(cert: x509.Certificate) -> dict[str, Any]:
    def attr(oid):
        v = cert.subject.get_attributes_for_oid(oid)
        return v[0].value if v else None
    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(tzinfo=dt.timezone.utc)
    return {
        "common_name": attr(NameOID.COMMON_NAME),
        "team_id": attr(NameOID.ORGANIZATIONAL_UNIT_NAME),
        "serial": format(cert.serial_number, "X"),
        "expires": not_after.isoformat(),
        "expired": not_after < dt.datetime.now(dt.timezone.utc),
    }


def inspect_p12(path: str, password: str) -> dict[str, Any]:
    data = Path(path).expanduser().read_bytes()
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(data, password.encode())
    except ValueError as e:
        raise BridgeError(f"Can't open {path}: wrong password or not a PKCS#12 file ({e})")
    if cert is None:
        raise BridgeError("The .p12 contains no certificate")
    return {"has_private_key": key is not None, **cert_summary(cert), "extra_certs": len(extra or [])}


def parse_mobileprovision(data: bytes) -> dict[str, Any]:
    start, end = data.find(b"<?xml"), data.find(b"</plist>")
    if start < 0 or end < 0:
        raise BridgeError("Not a .mobileprovision file (no embedded plist)")
    return plistlib.loads(data[start:end + len(b"</plist>")])


def profile_summary(data: bytes) -> dict[str, Any]:
    p = parse_mobileprovision(data)
    ent = p.get("Entitlements", {})
    app_id = ent.get("application-identifier", "")
    certs = []
    for der in p.get("DeveloperCertificates", []):
        try:
            certs.append(cert_summary(x509.load_der_x509_certificate(der)))
        except Exception:
            pass
    exp = p.get("ExpirationDate")
    if p.get("ProvisionsAllDevices"):
        kind = "enterprise"
    elif p.get("ProvisionedDevices"):
        kind = "development" if ent.get("get-task-allow") else "ad-hoc"
    else:
        kind = "app-store"
    return {
        "name": p.get("Name"),
        "uuid": p.get("UUID"),
        "team_id": (p.get("TeamIdentifier") or [None])[0],
        "team_name": p.get("TeamName"),
        "bundle_id": app_id.split(".", 1)[1] if "." in app_id else app_id,
        "kind": kind,
        "devices": len(p.get("ProvisionedDevices", [])),
        "expires": exp.isoformat() if exp else None,
        "expired": bool(exp and exp.replace(tzinfo=None) < dt.datetime.utcnow()),
        "entitlements": sorted(ent.keys()),
        "certificates": certs,
    }
