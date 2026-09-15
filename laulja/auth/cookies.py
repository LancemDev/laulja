from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

"""
Loads YouTube cookies from a Netscape cookies.txt, a Cookie header string, or JSON.
Mirrors CookieFileParser.cs from the original C# project.
"""

AUTH_COOKIE_NAMES = {"SAPISID", "__Secure-3PAPISID", "__Secure-1PAPISID"}


@dataclass
class Cookie:
    name: str
    value: str
    domain: str = ".youtube.com"
    path: str = "/"
    secure: bool = True
    expires: Optional[int] = None  # unix timestamp, if known


def create_cookie(
    name: str,
    value: str,
    domain: Optional[str] = None,
    path: str = "/",
    secure: bool = True,
) -> Cookie:
    domain = (domain or ".youtube.com").strip()
    if not domain.startswith(".") and "youtube" in domain.lower():
        domain = "." + domain.lstrip(".")
    return Cookie(name=name, value=value, domain=domain, path=path or "/", secure=secure)


def has_auth_cookies(cookies: List[Cookie]) -> bool:
    return any(c.name in AUTH_COOKIE_NAMES for c in cookies)


def cookies_to_header(cookies: List[Cookie]) -> str:
    # A cookie jar can hold more than one row for the same identity cookie (e.g. a leftover
    # from a previous sign-in alongside the current one, or duplicate host-scoped rows that
    # collapse to the same name here) — sending both confuses which account SAPISIDHASH gets
    # computed from vs. which one the server actually session-binds to, which is exactly what
    # showed up as an empty library after switching accounts. Last one in `cookies` wins,
    # matching create_cookie's/the callers' newest-first ordering.
    deduped = {c.name: c for c in cookies}
    return "; ".join(f"{c.name}={c.value}" for c in deduped.values())


def parse_header(cookie_header: str) -> List[Cookie]:
    cookies: List[Cookie] = []
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        cookies.append(create_cookie(name, value))
    return cookies


def _looks_like_netscape(text: str) -> bool:
    lowered = text.lower()
    return "\t" in text or "netscape" in lowered or "#httponly_" in lowered


def parse_netscape(text: str) -> List[Cookie]:
    cookies: List[Cookie] = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if not line.strip() or line.startswith("#"):
            # Netscape httpOnly marker: #HttpOnly_.youtube.com ...
            if not line.lower().startswith("#httponly_"):
                continue
            line = line[len("#HttpOnly_"):]

        cols = line.split("\t")
        if len(cols) < 7:
            continue

        domain, _flag, path, secure, expires, name, value = cols[:7]
        name = name.strip()
        if not name:
            continue

        cookie = create_cookie(
            name, value.strip(), domain.strip(), path.strip(), secure.strip().upper() == "TRUE"
        )
        try:
            expires_unix = int(expires)
            if expires_unix > 0:
                cookie.expires = expires_unix
        except ValueError:
            pass

        cookies.append(cookie)
    return cookies


def parse_json(text: str) -> List[Cookie]:
    data = json.loads(text)
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    if isinstance(data, str):
        return parse_header(data)
    if not isinstance(data, list):
        raise ValueError(
            'Cookie JSON must be an array, {"cookies":[...]}, or a cookie header string.'
        )

    cookies: List[Cookie] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("Name")
        value = item.get("value", item.get("Value"))
        if not name or value is None:
            continue

        domain = item.get("domain") or item.get("Domain") or ".youtube.com"
        path = item.get("path") or item.get("Path") or "/"
        secure = item.get("secure", item.get("Secure", item.get("isSecure", item.get("IsSecure", True))))

        cookies.append(create_cookie(str(name), str(value), str(domain), str(path), bool(secure)))
    return cookies


def parse_file(path: str | Path) -> List[Cookie]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Cookie file not found: {p}")

    text = p.read_text().strip()
    if not text:
        return []

    if text.startswith("[") or text.startswith("{"):
        return parse_json(text)
    if _looks_like_netscape(text):
        return parse_netscape(text)
    return parse_header(text)
