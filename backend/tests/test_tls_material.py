"""W04 tests for the self-signed 127.0.0.1 certificate that uvicorn binds."""

from __future__ import annotations

import datetime as dt
import ipaddress
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from expirymanager.security import tls


def test_generates_certificate_with_the_required_names(tmp_path) -> None:
    material = tls.ensure_tls_material(tmp_path / "tls")
    assert material.regenerated is True
    assert material.key_path.name == "server.key"
    assert material.cert_path.name == "server.crt"

    certificate = tls.load_certificate(material.cert_path)
    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    assert common_names[0].value == "127.0.0.1"
    assert certificate.issuer == certificate.subject

    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert list(san.get_values_for_type(x509.IPAddress)) == [ipaddress.ip_address("127.0.0.1")]
    assert list(san.get_values_for_type(x509.DNSName)) == ["localhost"]


def test_certificate_extensions_match_the_security_document(tmp_path) -> None:
    material = tls.ensure_tls_material(tmp_path / "tls")
    certificate = tls.load_certificate(material.cert_path)

    basic = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.value.ca is False
    assert basic.critical is True

    usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    assert usage.digital_signature is True
    assert usage.key_encipherment is True
    assert usage.key_cert_sign is False

    eku = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]


def test_validity_is_about_one_year_and_backdated(tmp_path) -> None:
    material = tls.ensure_tls_material(tmp_path / "tls")
    certificate = tls.load_certificate(material.cert_path)
    now = dt.datetime.now(dt.timezone.utc)
    assert certificate.not_valid_before_utc < now
    span = certificate.not_valid_after_utc - certificate.not_valid_before_utc
    assert dt.timedelta(days=364) < span < dt.timedelta(days=367)
    assert material.not_valid_after == certificate.not_valid_after_utc


def test_both_files_are_written_0600(tmp_path) -> None:
    material = tls.ensure_tls_material(tmp_path / "tls")
    assert material.key_path.stat().st_mode & 0o777 == 0o600
    assert material.cert_path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "tls").stat().st_mode & 0o777 == 0o700


def test_existing_valid_material_is_reused(tmp_path) -> None:
    directory = tmp_path / "tls"
    first = tls.ensure_tls_material(directory)
    fingerprint = tls.load_certificate(first.cert_path).fingerprint(hashes.SHA256())
    second = tls.ensure_tls_material(directory)
    assert second.regenerated is False
    assert tls.load_certificate(second.cert_path).fingerprint(hashes.SHA256()) == fingerprint


def test_expired_certificate_is_regenerated(tmp_path, monkeypatch) -> None:
    directory = tmp_path / "tls"
    tls.ensure_tls_material(directory, validity_days=1, renew_within_days=0)
    original = (directory / "server.crt").read_bytes()

    # Move the clock past the short lived certificate rather than waiting for it.
    real_now = tls._utcnow()
    monkeypatch.setattr(tls, "_utcnow", lambda: real_now + dt.timedelta(days=2))
    assert (
        tls.regeneration_reason(directory / "server.key", directory / "server.crt")
        == "expired"
    )
    renewed = tls.ensure_tls_material(directory)
    assert renewed.regenerated is True
    assert (directory / "server.crt").read_bytes() != original


def test_missing_certificate_is_regenerated(tmp_path) -> None:
    directory = tmp_path / "tls"
    tls.ensure_tls_material(directory)
    (directory / "server.crt").unlink()
    assert tls.regeneration_reason(directory / "server.key", directory / "server.crt") == "missing"
    assert tls.ensure_tls_material(directory).regenerated is True


def test_loose_permissions_force_regeneration(tmp_path) -> None:
    directory = tmp_path / "tls"
    material = tls.ensure_tls_material(directory)
    material.key_path.chmod(0o644)
    assert (
        tls.regeneration_reason(material.key_path, material.cert_path) == "loose_permissions"
    )
    assert tls.ensure_tls_material(directory).regenerated is True
    assert material.key_path.stat().st_mode & 0o777 == 0o600


def test_mismatched_key_forces_regeneration(tmp_path) -> None:
    directory = tmp_path / "tls"
    material = tls.ensure_tls_material(directory)
    other_key_pem, _ = tls.generate_self_signed()
    material.key_path.chmod(0o600)
    material.key_path.write_bytes(other_key_pem)
    material.key_path.chmod(0o600)
    assert tls.regeneration_reason(material.key_path, material.cert_path) == "key_mismatch"


def test_unparseable_certificate_forces_regeneration(tmp_path) -> None:
    directory = tmp_path / "tls"
    material = tls.ensure_tls_material(directory)
    material.cert_path.write_bytes(b"not a certificate")
    material.cert_path.chmod(0o600)
    assert tls.regeneration_reason(material.key_path, material.cert_path) == "unparseable"


def test_force_regenerates_even_when_valid(tmp_path) -> None:
    directory = tmp_path / "tls"
    first = tls.ensure_tls_material(directory)
    before = first.cert_path.read_bytes()
    assert tls.ensure_tls_material(directory, force=True).regenerated is True
    assert first.cert_path.read_bytes() != before


def test_material_loads_into_an_ssl_context(tmp_path) -> None:
    """The end that matters: uvicorn hands these two paths to the ssl module."""
    material = tls.ensure_tls_material(tmp_path / "tls")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(material.cert_path), keyfile=str(material.key_path))


def test_private_key_is_written_unencrypted_pkcs8(tmp_path) -> None:
    material = tls.ensure_tls_material(tmp_path / "tls")
    data = material.key_path.read_bytes()
    assert data.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert serialization.load_pem_private_key(data, password=None) is not None


def test_notice_and_summary_are_plain_text(tmp_path) -> None:
    material = tls.ensure_tls_material(tmp_path / "tls")
    summary = tls.certificate_summary(material)
    assert "127.0.0.1" in tls.SELF_SIGNED_NOTICE
    assert "CN=127.0.0.1" in summary
    # The private key path is on the never-log list, so it must not appear in either line.
    assert "server.key" not in tls.SELF_SIGNED_NOTICE
    assert "server.key" not in summary


@pytest.mark.parametrize("filename", ["server.key", "server.crt"])
def test_filenames_are_the_documented_ones(filename, tmp_path) -> None:
    material = tls.ensure_tls_material(tmp_path / "tls")
    assert (material.key_path.parent / filename).exists()


def test_accepts_a_paths_like_object(tmp_path) -> None:
    """paths.ensure() hands over its Paths instance rather than a bare directory."""

    class PathsLike:
        tls_dir = tmp_path / "tls"

    material = tls.ensure_tls_material(PathsLike())
    assert material.cert_path == tmp_path / "tls" / "server.crt"
    assert material.regenerated is True


def test_real_paths_seam_produces_usable_material(tmp_path) -> None:
    paths = pytest.importorskip("expirymanager.paths")
    resolved = paths.ensure(tmp_path / "data")
    assert resolved.tls_key.exists()
    assert resolved.tls_cert.exists()
    certificate = tls.load_certificate(resolved.tls_cert)
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert ipaddress.ip_address("127.0.0.1") in san.get_values_for_type(x509.IPAddress)
