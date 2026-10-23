"""Throwaway X.509 PKI for tests (needs the dev-only `cryptography` package)."""

from __future__ import annotations

import datetime
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@dataclass
class CA:
    name: x509.Name
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate
    path: Path


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def make_ca(tmp: Path, cn: str) -> CA:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - datetime.timedelta(minutes=5))
        .not_valid_after(_now() + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    path = tmp / f"{cn.replace(' ', '-')}.crt"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return CA(name, key, cert, path)


def issue(
    ca: CA,
    tmp: Path,
    stem: str,
    *,
    cn: str,
    uris: tuple[str, ...] = (),
    dns: tuple[str, ...] = (),
    ips: tuple[str, ...] = (),
    server: bool = False,
) -> tuple[Path, Path]:
    """Issue a leaf cert; returns (cert_path, key_path) with the key at mode 0600."""
    key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = [x509.UniformResourceIdentifier(u) for u in uris]
    sans += [x509.DNSName(d) for d in dns]
    sans += [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    b = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(ca.name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - datetime.timedelta(minutes=5))
        .not_valid_after(_now() + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
    )
    if sans:
        b = b.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    cert = b.sign(ca.key, hashes.SHA256())
    cert_path = tmp / f"{stem}.crt"
    key_path = tmp / f"{stem}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)
    return cert_path, key_path


def server_cert(ca: CA, tmp: Path, stem: str = "central") -> tuple[Path, Path]:
    return issue(ca, tmp, stem, cn="localhost", dns=("localhost",), ips=("127.0.0.1",), server=True)


def node_cert(ca: CA, tmp: Path, node_uuid: str, stem: str = "node") -> tuple[Path, Path]:
    return issue(ca, tmp, stem, cn=node_uuid, uris=(f"urn:uuid:{node_uuid}",))
