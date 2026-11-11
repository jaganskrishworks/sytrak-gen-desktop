"""Secret files: the NFC signing key, the NFC password secret and the Google service account key.

Each one is picked in Settings from a local file (Browse…) or downloaded from a URL (From URL…).
A private copy is kept in <data folder>/secrets/ so the original can be deleted.
Lookup order: the configured path → <app folder>/secrets/<pattern> → <data folder>/secrets/<name>.
File contents are never logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from ..paths import app_dir, user_data_dir
from . import nfc_sticker as sticker

log = logging.getLogger(__name__)

MAX_DOWNLOAD = 64 * 1024
# Public point of the mock's dev_nfc_key.pem (key 1): stickers signed with it pass dev mappers only.
DEV_PUBLIC_KEYS = {"0429A81CC4338842ADB7EA745987BBE337C2A794D389491786BE6CC0AF50E2AB787A758C48F655A8282F8399DB97DB4AAF"}


class SecretError(Exception):
    pass


@dataclass(frozen=True)
class SecretKind:
    title: str
    file_name: str      # private copy in <data folder>/secrets/
    pattern: str        # looked for in <app folder>/secrets/
    filetypes: tuple

    def validate(self, data: bytes) -> str:
        """Raise SecretError if ``data`` is not this kind of secret; return a short description."""
        return _VALIDATORS[self.file_name](data)


SIGNING_KEY = SecretKind("NFC signing key", "nfc_signing_key.pem", "*.pem", (("PEM key", "*.pem"), ("All files", "*.*")))
PASSWORD_SECRET = SecretKind("NFC password secret", "nfc_password.secret", "*.secret", (("Secret", "*.secret"), ("All files", "*.*")))
GOOGLE_KEY = SecretKind("Google service account key", "google_service_account.json", "*.json", (("JSON key", "*.json"), ("All files", "*.*")))


def secrets_dir() -> Path:
    path = user_data_dir() / "secrets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def find(kind: SecretKind, configured: str = "") -> Path | None:
    candidates = [Path(configured)] if configured else []
    candidates += sorted((app_dir() / "secrets").glob(kind.pattern))
    candidates.append(secrets_dir() / kind.file_name)
    if kind is GOOGLE_KEY:
        candidates.append(user_data_dir() / kind.file_name)   # where older versions kept it
    return next((p for p in candidates if p.is_file()), None)


def _private_copy(kind: SecretKind, data: bytes) -> Path:
    target = secrets_dir() / kind.file_name
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def import_file(kind: SecretKind, source: str | Path) -> tuple[Path, str]:
    """Validate a local file and keep a private copy. Returns (copy path, description)."""
    try:
        data = Path(source).read_bytes()
    except OSError as exc:
        raise SecretError(f"Cannot read {source}: {exc.strerror or exc}") from exc
    if len(data) > MAX_DOWNLOAD:
        raise SecretError(f"{Path(source).name} is too large for a {kind.title}")
    info = kind.validate(data)
    path = _private_copy(kind, data)
    log.info("%s imported from a local file (%s)", kind.title, info)
    return path, info


def download(kind: SecretKind, url: str, timeout: float = 20) -> tuple[Path, str]:
    """Download, validate and keep a private copy. Returns (copy path, description)."""
    url = url.strip()
    if not re.match(r"https?://", url, re.I):
        raise SecretError("Enter an http(s):// URL")
    import requests
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        if resp.status_code != 200:
            raise SecretError(f"The server answered {resp.status_code} {resp.reason}")
        data = resp.raw.read(MAX_DOWNLOAD + 1, decode_content=True)
    except requests.RequestException as exc:
        raise SecretError(f"Download failed: {exc.__class__.__name__}") from exc
    if len(data) > MAX_DOWNLOAD:
        raise SecretError(f"The file is larger than {MAX_DOWNLOAD // 1024} KB - not a {kind.title}")
    try:
        info = kind.validate(data)
    except SecretError as exc:
        hint = " (got a web page - use a direct download link)" if data.lstrip()[:1] == b"<" else ""
        raise SecretError(f"{exc}{hint}") from exc
    path = _private_copy(kind, data)
    log.info("%s downloaded from %s (%s)", kind.title, re.sub(r"[?#].*", "", url), info)
    return path, info


# ------------------------------------------------------------- validators ---

def _load_key(data: bytes) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(data, None)
    except (ValueError, TypeError) as exc:
        raise SecretError("Not an unencrypted PEM private key") from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP192R1):
        raise SecretError("The key is not an ECDSA P-192 (secp192r1) key")
    return key


def _load_secret(data: bytes) -> bytes:
    text = data.decode("ascii", errors="replace").strip()
    if not re.fullmatch(r"[0-9A-Fa-f]+", text) or len(text) % 2:
        raise SecretError("The password secret must be one line of hex")
    secret = bytes.fromhex(text)
    if len(secret) < 16:
        raise SecretError(f"The password secret is only {len(secret)} bytes (16 or more needed)")
    return secret


def _key_info(data: bytes) -> str:
    point = sticker.public_point(_load_key(data).public_key())
    return f"P-192, public key {point[:12]}…{point[-6:]}" + ("  (DEV key)" if point in DEV_PUBLIC_KEYS else "")


def _secret_info(data: bytes) -> str:
    return f"{len(_load_secret(data))} bytes"


def _google_info(data: bytes) -> str:
    try:
        info = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SecretError("Not a JSON file") from exc
    if not isinstance(info, dict) or info.get("type") != "service_account" or not info.get("private_key"):
        raise SecretError("Not a Google service account key (type service_account)")
    return info.get("client_email", "service account")


_VALIDATORS = {SIGNING_KEY.file_name: _key_info, PASSWORD_SECRET.file_name: _secret_info, GOOGLE_KEY.file_name: _google_info}


# ------------------------------------------------------------- admin view ---
# Read on demand for the Settings / Verify eye buttons. Never logged, never sent to the phone.

def _read(kind: SecretKind, configured: str) -> bytes:
    path = find(kind, configured)
    if path is None:
        raise SecretError(f"No {kind.title} found")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SecretError(f"Cannot read {path}: {exc.strerror or exc}") from exc
    kind.validate(data)
    return data


def reveal_signing_key(configured: str = "") -> str:
    """Private key: the 24-byte scalar (hex) and the PEM file."""
    data = _read(SIGNING_KEY, configured)
    scalar = _load_key(data).private_numbers().private_value.to_bytes(24, "big").hex().upper()
    return f"Private value  {scalar}\n" + data.decode("ascii", errors="replace").strip()


def public_key_hex(configured: str = "") -> str:
    """Uncompressed public point - not secret: the mappers need it as the trusted key."""
    return sticker.public_point(_load_key(_read(SIGNING_KEY, configured)).public_key())


def reveal_password_secret(configured: str = "") -> str:
    return _load_secret(_read(PASSWORD_SECRET, configured)).hex().upper()


def reveal_google_key(configured: str = "") -> str:
    info = json.loads(_read(GOOGLE_KEY, configured).decode("utf-8"))
    return (f"Service account  {info.get('client_email', '')}\nProject  {info.get('project_id', '')}\n"
            f"Private key ID  {info.get('private_key_id', '')}\n{str(info.get('private_key', '')).strip()}")


def tag_password_text(uid: str, secret_file: str = "") -> str:
    """'PWD 998A4881   PACK 1B77' for a tag UID (to check or un-protect a tag by hand)."""
    uid = re.sub(r"[^0-9A-Fa-f]", "", uid).upper()
    if not sticker.is_hex(uid, 7):
        raise SecretError("Enter the tag UID: 14 hex characters, e.g. 04C34DEA0B1390")
    pwd, pack = sticker.tag_password(_load_secret(_read(PASSWORD_SECRET, secret_file)), uid)
    return f"UID {uid}   PWD {pwd.hex().upper()}   PACK {pack.hex().upper()}"


# ------------------------------------------------------------------ signer ---

@dataclass(frozen=True)
class Signer:
    """The PC's signing "server": signs stickers and derives tag passwords."""
    key: ec.EllipticCurvePrivateKey
    key_id: int
    secret: bytes

    @property
    def public_hex(self) -> str:
        return sticker.public_point(self.key.public_key())

    @property
    def is_dev(self) -> bool:
        return self.public_hex in DEV_PUBLIC_KEYS

    @property
    def public_keys(self) -> dict[int, ec.EllipticCurvePublicKey]:
        return {self.key_id: self.key.public_key()}

    def ndef(self, uid: str, mac: str) -> bytes:
        """The NDEF TLV (page 4 onward) of a signed sticker for this UID + MAC."""
        return sticker.build_ndef(mac, sticker.sig_payload(self.key, self.key_id, uid, mac))

    def password(self, uid: str) -> tuple[bytes, bytes]:
        return sticker.tag_password(self.secret, uid)


def load_signer(key_file: str = "", secret_file: str = "", key_id: int = 1) -> Signer:
    """Load the configured key + secret; SecretError says what to fix in Settings."""
    key_path, secret_path = find(SIGNING_KEY, key_file), find(PASSWORD_SECRET, secret_file)
    missing = [k.title for k, p in ((SIGNING_KEY, key_path), (PASSWORD_SECRET, secret_path)) if p is None]
    if missing:
        raise SecretError(f"No {' and no '.join(missing)} - set it in Settings › NFC signing")
    if not 1 <= key_id <= 15:
        raise SecretError("Key ID must be 1-15 (Settings › NFC signing)")
    try:
        key = _load_key(key_path.read_bytes())
        secret = _load_secret(secret_path.read_bytes())
    except OSError as exc:
        raise SecretError(f"Cannot read a secret file: {exc.strerror or exc}") from exc
    return Signer(key, key_id, secret)
