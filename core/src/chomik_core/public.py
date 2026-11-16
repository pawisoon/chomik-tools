"""Walking other users' public folders.

Subfolders come from the public HTML page (no login needed); file lists come
from the Download SOAP call made with *your* client's token.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Iterator
from urllib.parse import urlparse

import requests

from .client import USER_AGENT, ChomikClient
from .models import FolderListing
from .names import path_from_url

log = logging.getLogger("chomik_core")

_FOLDERS_DIV = re.compile(r'<div id="foldersList">(.*?)</div>', re.DOTALL)
_HREF = re.compile(r'href="(.*?)"')


def list_public_subfolders(url: str, session: requests.Session | None = None, timeout: float = 30) -> list[str]:
    """Absolute URLs of the direct subfolders shown on a public chomikuj folder page."""
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", USER_AGENT)
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    return parse_subfolder_links(r.text, url)


def parse_subfolder_links(page_html: str, page_url: str) -> list[str]:
    m = _FOLDERS_DIV.search(page_html)
    if not m:
        return []
    scheme = urlparse(page_url).scheme or "https"
    scheme = scheme if scheme in ("http", "https") else "https"
    out: list[str] = []
    for href in _HREF.findall(m.group(1)):
        full = href if href.startswith(("http://", "https://")) else f"{scheme}://chomikuj.pl{href}"
        leaf = os.path.basename(urlparse(full).path)
        # Same rule as the downloader: a dot in the last segment means a file link.
        if "." in leaf:
            continue
        if full not in out:
            out.append(full)
    return out


def walk_public(
    client: ChomikClient,
    url: str,
    *,
    max_depth: int | None = None,
    session: requests.Session | None = None,
) -> Iterator[tuple[tuple[str, ...], FolderListing]]:
    """Yield (relative_path, listing) for the folder at `url` and every subfolder.

    `relative_path` is the tuple of folder names below `url` (empty for `url` itself),
    ready to mirror under a destination root.
    """
    yield from _walk(client, url, (), 0, max_depth, session or client.session, set())


def _walk(client, url, rel, depth, max_depth, session, seen):
    path = path_from_url(url)
    if path in seen:
        return
    seen.add(path)
    yield rel, client.list_files(path)
    if max_depth is not None and depth >= max_depth:
        return
    for sub in list_public_subfolders(url, session):
        name = _folder_name(sub)
        yield from _walk(client, sub, (*rel, name), depth + 1, max_depth, session, seen)


def _folder_name(url: str) -> str:
    from .names import decode_segment

    return decode_segment(os.path.basename(urlparse(url).path.rstrip("/")))
