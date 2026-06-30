"""SIP digest authentication helpers."""

from __future__ import annotations

import hashlib
import re
from secrets import token_hex


_PARAM_RE = re.compile(r'([a-zA-Z0-9_-]+)=("([^"\\]*(?:\\.[^"\\]*)*)"|[^,\s]+)')


def parse_digest_challenge(value: str) -> dict[str, str]:
    raw = (value or "").strip()
    if raw.lower().startswith("digest "):
        raw = raw[7:].strip()
    out: dict[str, str] = {}
    for match in _PARAM_RE.finditer(raw):
        key = match.group(1).lower()
        val = match.group(3) if match.group(3) is not None else match.group(2)
        out[key] = val.replace('\\"', '"') if val is not None else ""
    return out


def build_digest_authorization(
    *,
    challenge_header: str,
    username: str,
    password: str,
    method: str,
    uri: str,
    auth_username: str = "",
) -> str:
    challenge = parse_digest_challenge(challenge_header)
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    algorithm = (challenge.get("algorithm") or "MD5").upper()
    qop_raw = challenge.get("qop", "")
    qops = [part.strip() for part in qop_raw.split(",") if part.strip()]
    qop = "auth" if "auth" in qops else (qops[0] if qops else "")
    digest_user = auth_username or username
    if algorithm not in {"MD5", ""}:
        raise ValueError(f"unsupported SIP digest algorithm {algorithm}")
    ha1 = hashlib.md5(f"{digest_user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method.upper()}:{uri}".encode()).hexdigest()
    params = {
        "username": digest_user,
        "realm": realm,
        "nonce": nonce,
        "uri": uri,
        "response": "",
        "algorithm": "MD5",
    }
    if qop:
        cnonce = token_hex(8)
        nc = "00000001"
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        params.update({"qop": qop, "nc": nc, "cnonce": cnonce, "response": response})
    else:
        params["response"] = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    rendered = []
    for key, val in params.items():
        if key in {"algorithm", "qop", "nc"}:
            rendered.append(f"{key}={val}")
        else:
            rendered.append(f'{key}="{str(val).replace(chr(34), "")}"')
    return "Digest " + ", ".join(rendered)

