"""Name sanitising and chomikuj path encoding."""
from __future__ import annotations

import re
from urllib.parse import urlparse

_FORBIDDEN = '\\/:*?"<>|'
_LOCAL_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f/\\]')
_PATH_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~")


def remote_dirname(name: str) -> str:
    """Folder name as chomikuj will store it (forbidden chars dropped, 256 max)."""
    name = name[:256]
    for c in _FORBIDDEN:
        name = name.replace(c, "")
    return name.strip(". ")


def remote_filename(name: str) -> str:
    """File name as chomikuj will store it (forbidden chars -> space, 256 max)."""
    name = name[:256]
    for c in _FORBIDDEN:
        name = name.replace(c, " ")
    return re.sub(r"\s+", " ", name).strip()


def local_filename(name: str) -> str:
    """Name safe to write on macOS / Windows / Linux."""
    return _LOCAL_UNSAFE.sub("_", name).lstrip(". ") or "_"


def encode_segment(segment: str) -> str:
    """Encode one path segment the way chomikuj URLs do: space -> '+', other unsafe bytes -> '*xx'.

    Example: "Audi Obsługi" -> "Audi+Obs*c5*82ugi".
    """
    out = []
    for ch in segment:
        if ch == " ":
            out.append("+")
        elif ch in _PATH_SAFE:
            out.append(ch)
        else:
            out.extend(f"*{b:02x}" for b in ch.encode("utf-8"))
    return "".join(out)


def decode_segment(segment: str) -> str:
    """Inverse of encode_segment. Also accepts %xx (browsers sometimes rewrite)."""
    raw = bytearray()
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch in "*%" and _is_hex(segment[i + 1 : i + 3]):
            raw.append(int(segment[i + 1 : i + 3], 16))
            i += 3
            continue
        raw.extend(b" " if ch == "+" else ch.encode("utf-8"))
        i += 1
    return raw.decode("utf-8", errors="replace")


def _is_hex(s: str) -> bool:
    return len(s) == 2 and all(c in "0123456789abcdefABCDEF" for c in s)


def chomik_path(account: str, parts: list[str] | tuple[str, ...]) -> str:
    """Build the '/account/a/b' id the Download SOAP call accepts."""
    return "/" + "/".join([account, *[encode_segment(p) for p in parts if p]])


def path_from_url(url: str) -> str:
    """Normalise a chomikuj URL (absolute or '/user/...') to its path, e.g. '/user/Folder'."""
    parsed = urlparse(url if "://" in url else "https://chomikuj.pl/" + url.lstrip("/"))
    path = "/" + (parsed.path or "/").strip("/")
    return path


def split_path(path: str) -> tuple[str, list[str]]:
    """'/user/a+b/c' -> ('user', ['a b', 'c']) with segments decoded."""
    segs = [s for s in path.strip("/").split("/") if s]
    if not segs:
        raise ValueError(f"empty chomikuj path: {path!r}")
    return segs[0], [decode_segment(s) for s in segs[1:]]
