"""Match chomikuj search hits to what Sonarr/Radarr asked for, and name them so the arr parser accepts them.

Chomikuj file names are free-form ("Big Buck Bunny 1080p (2008).avi",
"Serial 1x02 lektor.mkv"). Sonarr and Radarr only accept scene-style titles,
so each accepted hit gets a synthetic release name built from the title the
arr asked for plus whatever year / episode / quality tokens the file name
carries, e.g. "Big.Buck.Bunny.2008.1080p.DVDRip.Polish-Chomikuj".
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from chomik_core import SearchHit, split_path

VIDEO_EXT = {"mkv", "mp4", "avi", "m4v", "mov", "wmv", "ts", "m2ts", "mpg", "mpeg", "rmvb", "webm", "divx", "ogm"}

_SXXEYY = re.compile(r"(?i)(?<![a-z0-9])s(\d{1,2})[ ._-]?e(\d{1,3})(?!\d)")
_NXNN = re.compile(r"(?i)(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?!\d)")
_SEASON_WORD = re.compile(r"(?i)(?<![a-z0-9])(?:s|sezon|season)[ ._-]?(\d{1,2})(?!\d)")
_EPISODE_WORD = re.compile(r"(?i)(?<![a-z0-9])(?:odcinek|odc|episode|ep|e)[ ._-]?(\d{1,3})(?!\d)")
_YEAR = re.compile(r"(?<!\d)(19[3-9]\d|20[0-4]\d)(?!\d)")
_RES = re.compile(r"(?i)(?<![a-z0-9])(2160p|4k|uhd|1080p|1080i|720p|576p|480p)(?![a-z0-9])")
_SOURCES = [
    (re.compile(r"(?i)remux"), "BluRay.REMUX"),
    (re.compile(r"(?i)blu-?ray|bdremux"), "BluRay"),
    (re.compile(r"(?i)(?<![a-z])b[dr]-?rip"), "BDRip"),
    (re.compile(r"(?i)web-?dl"), "WEB-DL"),
    (re.compile(r"(?i)web-?rip"), "WEBRip"),
    (re.compile(r"(?i)(?<![a-z])web(?![a-z])"), "WEB-DL"),
    (re.compile(r"(?i)hdtv"), "HDTV"),
    (re.compile(r"(?i)dvd-?rip"), "DVDRip"),
    (re.compile(r"(?i)(?<![a-z])dvd(?:5|9|r)?(?![a-z])"), "DVD"),
    (re.compile(r"(?i)(?<![a-z])(?:hd)?(?:ts|telesync)(?![a-z])"), "TS"),
    (re.compile(r"(?i)(?<![a-z])(?:hd)?cam(?:rip)?(?![a-z])"), "CAM"),
    (re.compile(r"(?i)hdrip"), "HDRip"),
]
_POLISH = re.compile(r"(?i)(?<![a-z])(pl|lektor|dubbing|dubbingpl|dubpl|napisy|polish|polski|lekpl|plsub)(?![a-z])")
_STOP = {"the", "a", "an", "and", "of", "i", "w", "na"}


def fold(text: str) -> str:
    """Lower-case ASCII: 'Łódź – Ćma' -> 'lodz cma'."""
    text = text.replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def words(text: str) -> list[str]:
    return [w for w in fold(text).split() if w]


@dataclass(frozen=True)
class Wanted:
    title: str  # as the arr sent it
    year: int | None = None
    season: int | None = None
    episode: int | None = None

    @property
    def is_tv(self) -> bool:
        return self.season is not None


def parse_query(q: str, *, season: str | int | None = None, ep: str | int | None = None,
                movie: bool = False) -> Wanted:
    """Split what an arr sends ('Big Buck Bunny 2008', or q + season/ep) into title / year / episode."""
    q = re.sub(r"[+]", " ", q or "").strip()
    s = int(season) if str(season or "").isdigit() else None
    e = int(ep) if str(ep or "").isdigit() else None
    m = _SXXEYY.search(q)
    if m and s is None:
        s, e = int(m.group(1)), int(m.group(2))
        q = (q[: m.start()] + q[m.end():]).strip()
    year = None
    y = re.search(r"\(?(19[3-9]\d|20[0-4]\d)\)?\s*$", q)
    if y and (movie or s is None) and y.start() > 0:
        year = int(y.group(1))
        q = q[: y.start()].strip()
    return Wanted(re.sub(r"\s+", " ", q).strip(" .-"), year, s, e)


def _episode_of(stem: str, context: str) -> tuple[int | None, int | None]:
    """(season, episode) from the file name, using the folder names for the season when the file has none."""
    m = _SXXEYY.search(stem) or _NXNN.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2))
    s = _SEASON_WORD.search(stem) or _SEASON_WORD.search(context)
    e = _EPISODE_WORD.search(stem)
    season = int(s.group(1)) if s else None
    return season, (int(e.group(1)) if e and season is not None else None)


@dataclass(frozen=True)
class Release:
    hit: SearchHit
    title: str  # synthetic scene-style name
    season: int | None
    episode: int | None
    year: int | None


def stem_and_ext(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    return (stem, ext.lower()) if dot and len(ext) <= 5 else (name, "")


def match(hit: SearchHit, wanted: Wanted, *, min_bytes: int = 0, max_bytes: int = 0) -> Release | None:
    """The Release for `hit` if it fits `wanted`, else None."""
    stem, ext = stem_and_ext(hit.name)
    if ext not in VIDEO_EXT:
        return None
    if min_bytes and hit.size_bytes and hit.size_bytes < min_bytes:
        return None
    if max_bytes and hit.size_bytes > max_bytes:
        return None
    try:
        _, folders = split_path(hit.folder_path)
    except ValueError:
        folders = []
    context = " ".join(folders[-2:])
    have = set(words(stem)) | set(words(context))
    need = [w for w in words(wanted.title) if w not in _STOP] or words(wanted.title)
    if not need or not all(w in have for w in need):
        return None

    season, episode = _episode_of(stem, context)
    year_m = _YEAR.search(stem) or _YEAR.search(context)
    year = int(year_m.group(1)) if year_m else None

    if wanted.is_tv:
        if season != wanted.season or episode is None:
            return None
        if wanted.episode is not None and episode != wanted.episode:
            return None
    else:
        if episode is not None:
            return None
        if wanted.year and year and abs(year - wanted.year) > 1:
            return None
        year = wanted.year or year

    return Release(hit, release_title(wanted, stem, ext, hit.size_bytes, season, episode, year),
                   season, episode, year)


def release_title(wanted: Wanted, stem: str, ext: str, size: int, season: int | None, episode: int | None,
                  year: int | None) -> str:
    parts = [re.sub(r"[^0-9A-Za-z]+", ".", unicodedata.normalize("NFKD", wanted.title.replace("ł", "l")
                                                                  .replace("Ł", "L")).encode("ascii", "ignore")
                    .decode()).strip(".")]
    if wanted.is_tv:
        parts.append(f"S{season:02d}E{episode:02d}")
    elif year:
        parts.append(str(year))
    res = _RES.search(stem)
    resolution = res.group(1).lower() if res else None
    if resolution in ("4k", "uhd"):
        resolution = "2160p"
    source = next((label for rx, label in _SOURCES if rx.search(stem)), None)
    if source is None:
        # No tag in the name: guess from resolution and container (old .avi rips are mostly DVD).
        sd_container = ext in ("avi", "rmvb", "mpg", "mpeg", "divx", "ogm")
        source = "DVDRip" if sd_container and resolution in (None, "576p", "480p") else "WEBRip"
    if resolution is None and source not in ("DVDRip", "DVD", "TS", "CAM"):
        gb = size / 1024**3
        big, mid = (1.5, 0.6) if wanted.is_tv else (3.5, 1.2)
        resolution = "1080p" if gb >= big else "720p" if gb >= mid else None
    parts += [p for p in (resolution, source) if p]
    if _POLISH.search(stem):
        parts.append("Polish")
    return ".".join(parts) + "-Chomikuj"
