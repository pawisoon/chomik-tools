"""Site-wide file search on chomikuj.pl (the public search page, no login).

  POST https://chomikuj.pl/action/SearchFiles
       FileName, FileType (all|video|image|music|document|archive|application),
       SizeFrom, SizeTo (MB, 0 = no limit), Extension, IsGallery=False, Page

Verified anonymously 2026-11-21: 25 rows per page, no anti-forgery token
needed. Rows use the same markup as folder listings (see web.parse_file_rows)
plus the owner and the containing folder. Partner ads are mixed into the list
and have no file id; they are skipped.

Sizes on this page are rounded ("152,4 MB"), so SearchHit.size_bytes is an
estimate. ChomikClient.resolve_hit lists the containing folder over SOAP to
get the exact size and the agreement a download needs.

This module never solves captchas: when the site asks for one it raises
CaptchaRequired.
"""
from __future__ import annotations

import html as htmllib
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import requests

from .errors import ProtocolError
from .names import chomik_path, decode_segment, path_from_url, split_path
from .web import (BASE, BROWSER_UA, _CAPTCHA_MARKUP, _HREF_TAIL, _RATE_LIMIT, _ROW_HREF, _ROW_ID, _ROW_SIZE,
                  _ROW_SPLIT, CaptchaRequired)

SEARCH_URL = BASE + "/action/SearchFiles"
FILE_TYPES = ("all", "video", "image", "music", "document", "archive", "application")


class SearchRateLimited(ProtocolError):
    """The site kept refusing searches as too frequent."""


@dataclass(frozen=True)
class SearchHit:
    id: str
    name: str  # full name with extension
    size_text: str  # as shown, e.g. "152,4 MB"
    size_bytes: int  # estimate parsed from size_text
    owner: str
    folder_path: str  # chomikuj path of the containing folder, e.g. "/user/Filmy/Big+Buck+Bunny"
    url: str  # https://chomikuj.pl/... page of the file
    date: datetime | None = None


@dataclass
class SearchPage:
    query: str
    page: int
    hits: list[SearchHit] = field(default_factory=list)
    has_next: bool = False


_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
_SIZE = re.compile(r"([\d\s .,]+)\s*([KMGT]?B)\b", re.I)


def parse_size(text: str) -> int:
    """'152,4 MB' -> bytes (approximate). 0 when unreadable."""
    m = _SIZE.search(text or "")
    if not m:
        return 0
    num = re.sub(r"[\s ]", "", m.group(1)).replace(",", ".")
    try:
        return int(float(num) * _UNITS[m.group(2).upper()])
    except ValueError:
        return 0


_MONTHS = {"sty": 1, "lut": 2, "mar": 3, "kwi": 4, "maj": 5, "cze": 6,
           "lip": 7, "sie": 8, "wrz": 9, "paź": 10, "paz": 10, "lis": 11, "gru": 12}
_DATE = re.compile(r"(\d{1,2})\s+(\w{3})\s+(\d{2,4})\s+(\d{1,2}):(\d{2})")


def parse_date(text: str) -> datetime | None:
    """'10 lut 13 4:19' -> datetime(2013, 2, 10, 4, 19)."""
    m = _DATE.search(text or "")
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    year = int(m.group(3))
    year += 2000 if year < 100 else 0
    try:
        return datetime(year, _MONTHS[m.group(2).lower()], int(m.group(1)), int(m.group(4)), int(m.group(5)))
    except ValueError:
        return None


_ROW_DATE = re.compile(r'class="date">([^<]+)<')
_ROW_OWNER = re.compile(r'class="searchedFileOwner">\s*<a href="/([^"/]+)"')
_ROW_DIR = re.compile(r'class="searchedFileDirectory">\s*<a href="([^"]+)"')
_NEXT = re.compile(r'<form action="/action/SearchFiles" class="right"')


def parse_search_page(body: str, query: str = "", page: int = 1) -> SearchPage:
    """Parse the HTML of one search results page."""
    if _CAPTCHA_MARKUP.search(body):
        raise CaptchaRequired("chomikuj.pl asked for a captcha on search; open the site in a browser and solve it")
    if 'id="SearchFilesForm"' not in body:
        if _RATE_LIMIT.search(body):
            raise SearchRateLimited("chomikuj.pl says: too many searches, wait a moment")
        raise ProtocolError("search: unexpected page (no search form in the response)")
    out = SearchPage(query, page, has_next=bool(_NEXT.search(body)))
    for chunk in _ROW_SPLIT.split(body)[1:]:
        m_id, m_href, m_dir = _ROW_ID.search(chunk), _ROW_HREF.search(chunk), _ROW_DIR.search(chunk)
        if not (m_id and m_href and m_dir):
            continue  # partner ad or broken row
        href = htmllib.unescape(m_href.group(1))
        seg = href.rstrip("/").rsplit("/", 1)[-1]
        tail = _HREF_TAIL.search(seg)
        name = decode_segment(seg[: tail.start()] if tail else seg) + ((tail.group(2) or "") if tail else "")
        owner, parts = split_path(path_from_url(htmllib.unescape(m_dir.group(1))))
        m_owner = _ROW_OWNER.search(chunk)
        size = _ROW_SIZE.search(chunk)
        size_text = size.group(1).strip() if size else ""
        date = _ROW_DATE.search(chunk)
        out.hits.append(SearchHit(
            id=m_id.group(1),
            name=name,
            size_text=size_text,
            size_bytes=parse_size(size_text),
            owner=m_owner.group(1) if m_owner else owner,
            folder_path=chomik_path(owner, parts),
            url=BASE + href,
            date=parse_date(date.group(1)) if date else None,
        ))
    return out


def hit_from_url(url: str) -> SearchHit:
    """A file page link ('https://chomikuj.pl/user/Folder/Name,123.avi(video)') as a SearchHit.

    Only id, name, owner and folder are known; size is 0 until resolved.
    """
    path = path_from_url(url)
    folder, _, seg = path.rstrip("/").rpartition("/")
    tail = _HREF_TAIL.search(seg)
    if not tail or not folder.strip("/"):
        raise ValueError(f"not a chomikuj.pl file link (expected ...,<id>.<ext>): {url!r}")
    owner, parts = split_path(folder)
    name = decode_segment(seg[: tail.start()]) + (tail.group(2) or "")
    return SearchHit(tail.group(1), name, "", 0, owner, chomik_path(owner, parts), BASE + path)


class Searcher:
    """Anonymous search session. Thread-safe; spaces requests at least `min_interval` seconds apart."""

    RATE_LIMIT_DELAYS: tuple[float, ...] = (10, 30)

    def __init__(self, session: requests.Session | None = None, *, min_interval: float = 2.0, timeout: float = 30,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8"})
        self.min_interval = min_interval
        self.timeout = timeout
        self._sleep, self._clock = sleep, clock
        self._lock = threading.Lock()
        self._last = float("-inf")

    def _wait_turn(self) -> None:
        gap = self._last + self.min_interval - self._clock()
        if gap > 0:
            self._sleep(gap)
        self._last = self._clock()

    def search(self, query: str, *, file_type: str = "all", size_from_mb: int = 0, size_to_mb: int = 0,
               extension: str = "", page: int = 1) -> SearchPage:
        query = (query or "").strip()
        if not query:
            raise ValueError("empty search query")
        if file_type not in FILE_TYPES:
            raise ValueError(f"file_type must be one of {', '.join(FILE_TYPES)}")
        form = {
            "FileName": query, "FileType": file_type, "IsGallery": "False",
            "SizeFrom": str(int(size_from_mb or 0)), "SizeTo": str(int(size_to_mb or 0)),
            "Extension": extension.lstrip("."), "Page": str(max(1, int(page))),
        }
        delays = (0.0, *self.RATE_LIMIT_DELAYS)
        with self._lock:
            for attempt, delay in enumerate(delays):
                if delay:
                    self._sleep(delay)
                self._wait_turn()
                try:
                    r = self.s.post(SEARCH_URL, data=form, timeout=self.timeout,
                                    headers={"Referer": BASE + "/action/Search"})
                except requests.RequestException as e:
                    raise ProtocolError(f"search: request failed: {e}") from e
                if r.status_code >= 400:
                    raise ProtocolError(f"search: HTTP {r.status_code}")
                try:
                    return parse_search_page(r.text, query, int(form["Page"]))
                except SearchRateLimited:
                    if attempt == len(delays) - 1:
                        raise
        raise AssertionError("unreachable")


_default: Searcher | None = None


def search_files(query: str, **kw) -> SearchPage:
    """Search with a shared module-level Searcher (keeps the request spacing across callers)."""
    global _default
    if _default is None:
        _default = Searcher()
    return _default.search(query, **kw)
