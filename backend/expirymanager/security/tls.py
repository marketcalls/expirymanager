"""The self-signed certificate uvicorn binds on 127.0.0.1:8000.

This exists for one reason: the Fyers app is registered with the redirect URI
``https://127.0.0.1:8000/fyers/callback`` and Fyers matches it exactly, so the server has to speak
https on that host and that port. The project is zero configuration, so it generates its own
certificate rather than asking anyone to produce one (SECURITY.md section 3a).

What it buys and does not buy is stated there too: it satisfies the broker requirement and makes
Secure cookies behave the same in development and in production, but the trust decision is the
user's own click-through on an untrusted certificate, so it is not a strong control. HSTS is never
sent, because HSTS on 127.0.0.1 would poison that origin for every other local development server
on the machine.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from expirymanager.security.kek import write_secret_file

KEY_FILENAME = "server.key"
CERT_FILENAME = "server.crt"

COMMON_NAME = "127.0.0.1"
SAN_IP = "127.0.0.1"
SAN_DNS = "localhost"

VALIDITY_DAYS = 365

# Renew before the certificate actually lapses, so a long running install never wakes up on an
# expired certificate mid-session.
RENEW_WITHIN_DAYS = 30

# Backdated slightly, because a browser rejects a certificate whose notBefore is in its own future
# and small clock differences are ordinary.
BACKDATE_MINUTES = 5

SELF_SIGNED_NOTICE = (
    "TLS: using a self-signed certificate for https://127.0.0.1:8000. "
    "The browser will warn on the first visit. That warning is expected, not a fault."
)


@dataclass(frozen=True, slots=True)
class TlsMaterial:
    """Where the key and certificate live, and whether this call had to create them."""

    key_path: Path
    cert_path: Path
    not_valid_after: dt.datetime
    regenerated: bool


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def load_certificate(cert_path: Path) -> x509.Certificate:
    """Parse a PEM certificate from disk."""
    return x509.load_pem_x509_certificate(Path(cert_path).read_bytes())


def generate_self_signed(
    *,
    common_name: str = COMMON_NAME,
    validity_days: int = VALIDITY_DAYS,
) -> tuple[bytes, bytes]:
    """Return (private key PEM, certificate PEM) for a fresh self-signed server certificate.

    EC P-256 rather than RSA 2048: same acceptance in every browser and in the Python ssl module,
    faster to generate on first run, which is a zero-config startup path the user is waiting on.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = _utcnow()
    not_before = now - dt.timedelta(minutes=BACKDATE_MINUTES)
    not_after = now + dt.timedelta(days=validity_days)

    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address(SAN_IP)),
                    x509.DNSName(SAN_DNS),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem


def regeneration_reason(
    key_path: Path,
    cert_path: Path,
    *,
    renew_within_days: int = RENEW_WITHIN_DAYS,
) -> str | None:
    """Why the material must be replaced, or None when it is fine to keep using.

    A plain string rather than an exception, because every one of these is a normal condition on a
    developer machine and the caller only needs it for one log line.
    """
    key_file = Path(key_path)
    cert_file = Path(cert_path)
    if not key_file.exists() or not cert_file.exists():
        return "missing"
    if key_file.stat().st_mode & 0o077 or cert_file.stat().st_mode & 0o077:
        # A loose private key is cheaper to replace than to reason about, since it is self-signed
        # and nothing else trusts it.
        return "loose_permissions"
    try:
        certificate = load_certificate(cert_file)
    except ValueError:
        return "unparseable"
    try:
        private_key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    except (ValueError, TypeError):
        return "unparseable_key"
    if private_key.public_key().public_numbers() != certificate.public_key().public_numbers():
        return "key_mismatch"
    now = _utcnow()
    if certificate.not_valid_after_utc <= now + dt.timedelta(days=renew_within_days):
        return "expired"
    if certificate.not_valid_before_utc > now:
        return "not_yet_valid"
    return None


def _resolve_directory(target: object) -> Path:
    """Accept either a directory or the Paths object that paths.ensure() hands over.

    paths.py calls this module with its own Paths instance, so reaching for tls_dir here keeps
    that seam working without either side importing the other's types.
    """
    tls_dir = getattr(target, "tls_dir", None)
    if tls_dir is not None:
        return Path(tls_dir)
    return Path(target)  # type: ignore[arg-type]


def ensure_tls_material(
    tls_dir: Path | str | object,
    *,
    validity_days: int = VALIDITY_DAYS,
    renew_within_days: int = RENEW_WITHIN_DAYS,
    force: bool = False,
) -> TlsMaterial:
    """Guarantee a usable key and certificate in ``tls_dir`` and return their paths.

    Called from paths.ensure() before uvicorn binds. Both files are written 0600 with O_EXCL and
    fsynced, along with the parent directory, exactly as the master key is.
    """
    directory = _resolve_directory(tls_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = directory / KEY_FILENAME
    cert_path = directory / CERT_FILENAME

    reason = "forced" if force else regeneration_reason(
        key_path, cert_path, renew_within_days=renew_within_days
    )
    if reason is None:
        certificate = load_certificate(cert_path)
        return TlsMaterial(
            key_path=key_path,
            cert_path=cert_path,
            not_valid_after=certificate.not_valid_after_utc,
            regenerated=False,
        )

    key_pem, cert_pem = generate_self_signed(validity_days=validity_days)
    write_secret_file(key_path, key_pem, overwrite=True)
    write_secret_file(cert_path, cert_pem, overwrite=True)
    certificate = x509.load_pem_x509_certificate(cert_pem)
    return TlsMaterial(
        key_path=key_path,
        cert_path=cert_path,
        not_valid_after=certificate.not_valid_after_utc,
        regenerated=True,
    )


def certificate_summary(material: TlsMaterial) -> str:
    """One safe line for the startup log. The private key path is on the never-log list."""
    return (
        f"TLS certificate for CN={COMMON_NAME} valid until "
        f"{material.not_valid_after.date().isoformat()}"
    )
