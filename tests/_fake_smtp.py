"""A tiny real-socket SMTP server for tests: EHLO, STARTTLS, AUTH PLAIN, DATA.

Not a mail server: just enough of RFC 5321/3207/4954 for smtplib, recording
what a client actually did on the wire (TLS before AUTH? which creds?).
"""

from __future__ import annotations

import base64
import datetime
import socket
import ssl
import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Received:
    mail_from: str
    rcpt_to: list[str]
    data: str
    tls: bool
    auth: tuple[str, str] | None


@dataclass
class FakeSMTP:
    tls_context: ssl.SSLContext | None = None  # None: no STARTTLS offered
    implicit_tls: bool = False  # SMTPS (port-465 style): TLS from the first byte
    fail_next: int = 0  # reply 451 to the next N MAIL FROM commands
    messages: list[Received] = field(default_factory=list)
    auth_attempts: list[tuple[str, str, bool]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop = True
        self._sock.close()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn: socket.socket) -> None:
        tls = False
        try:
            if self.implicit_tls:
                assert self.tls_context is not None
                conn = self.tls_context.wrap_socket(conn, server_side=True)
                tls = True
            f = conn.makefile("rwb")

            def say(line: str) -> None:
                f.write(line.encode() + b"\r\n")
                f.flush()

            say("220 fake ESMTP")
            mail_from, rcpts, auth = "", [], None
            while True:
                raw = f.readline()
                if not raw:
                    return
                line = raw.decode().rstrip("\r\n")
                verb = line.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    exts = ["250-fake"]
                    if self.tls_context is not None and not tls and not self.implicit_tls:
                        exts.append("250-STARTTLS")
                    exts.append("250 AUTH PLAIN")
                    for e in exts:
                        say(e)
                elif verb == "STARTTLS":
                    say("220 go ahead")
                    assert self.tls_context is not None
                    conn = self.tls_context.wrap_socket(conn, server_side=True)
                    tls = True
                    f = conn.makefile("rwb")
                elif verb == "AUTH":
                    parts = line.split()
                    blob = parts[2] if len(parts) > 2 else ""
                    if not blob:
                        say("334 ")
                        blob = f.readline().decode().strip()
                    _, user, pw = base64.b64decode(blob).decode().split("\0")
                    self.auth_attempts.append((user, pw, tls))
                    auth = (user, pw)
                    say("235 ok")
                elif verb == "MAIL":
                    if self.fail_next > 0:
                        self.fail_next -= 1
                        say("451 try again later")
                        continue
                    mail_from = line.split(":", 1)[1].strip().strip("<>")
                    rcpts = []
                    say("250 ok")
                elif verb == "RCPT":
                    rcpts.append(line.split(":", 1)[1].strip().strip("<>"))
                    say("250 ok")
                elif verb == "DATA":
                    say("354 end with .")
                    body = []
                    while True:
                        ln = f.readline().decode()
                        if ln in (".\r\n", ".\n", ""):
                            break
                        body.append(ln)
                    self.messages.append(Received(mail_from, rcpts, "".join(body), tls, auth))
                    say("250 queued")
                elif verb == "RSET":
                    say("250 ok")
                elif verb == "QUIT":
                    say("221 bye")
                    return
                else:
                    say("500 unknown")
        except (OSError, ssl.SSLError, AssertionError, ValueError):
            return
        finally:
            conn.close()


def make_ca_and_server_cert(tmp: Path) -> tuple[Path, ssl.SSLContext]:
    """Throwaway CA + 'localhost' server cert. Returns (ca_pem_path, server_ctx)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import ipaddress

    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test smtp ca")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
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
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        # Python 3.13+ sets VERIFY_X509_STRICT, which requires SKI/AKI.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = tmp / "smtp-ca.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path = tmp / "smtp-server.pem"
    cert_path.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
        + key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path)
    return ca_path, ctx
