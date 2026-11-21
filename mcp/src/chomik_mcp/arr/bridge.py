"""Sonarr/Radarr bridge: chomikuj.pl as a Newznab indexer plus a SABnzbd-compatible download client.

  /arr/newznab/api       indexer (add as "Generic Newznab" in Sonarr/Radarr or Prowlarr)
  /arr/newznab/nzb/<t>   tiny placeholder NZB naming one chomikuj file (signed, no API key needed)
  /arr/sabnzbd/api       download client (add as "SABnzbd", URL base /arr/sabnzbd)

Nothing here is usenet: the "NZB" only carries the chomikuj file id and
folder, and the download client fetches that file over the ChomikBox API,
paid from the configured account's transfer.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import format_datetime
from xml.sax.saxutils import escape

from chomik_core import ChomikError, SearchHit
from chomik_core.models import Balance
from chomik_core.search import Searcher
from chomik_core.web import CaptchaRequired

from .queue import Item, Queue
from .release import Release, Wanted, match, parse_query, stem_and_ext

log = logging.getLogger("chomik_mcp.arr")

NZB_NS = "http://www.newzbin.com/DTD/2003/nzb"
NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"
CAT_MOVIES, CAT_MOVIES_HD, CAT_TV, CAT_TV_HD = 2000, 2040, 5000, 5040
VERSION = "4.3.3"  # SABnzbd version we claim; Sonarr/Radarr need >= 0.7


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


@dataclass
class ArrConfig:
    """All from the environment (docker-compose / .env)."""

    account: str = field(default_factory=lambda: os.environ.get("ARR_ACCOUNT", ""))  # letter/username; "" = first
    complete_dir: str = field(default_factory=lambda: os.environ.get("ARR_COMPLETE_DIR", ""))
    min_size_mb: float = field(default_factory=lambda: _env_num("ARR_MIN_SIZE_MB", 100))
    max_size_mb: float = field(default_factory=lambda: _env_num("ARR_MAX_SIZE_MB", 0))  # 0 = no limit
    pages: int = field(default_factory=lambda: int(_env_num("ARR_SEARCH_PAGES", 2)))
    monthly_limit_gb: float = field(default_factory=lambda: _env_num("ARR_MONTHLY_LIMIT_GB", 0))  # 0 = no limit
    transfer_reserve_mb: float = field(default_factory=lambda: _env_num("ARR_TRANSFER_RESERVE_MB", 0))
    cache_seconds: float = field(default_factory=lambda: _env_num("ARR_CACHE_SECONDS", 900))


class Cancelled(Exception):
    pass


def _tv_cat(r: Release) -> int:
    return CAT_TV_HD if re.search(r"\.(720p|1080p|2160p)\.", r.title) else CAT_TV


def _movie_cat(r: Release) -> int:
    return CAT_MOVIES_HD if re.search(r"\.(720p|1080p|2160p)\.", r.title) else CAT_MOVIES


class ArrBridge:
    def __init__(self, tools, secret: str, api_key, *, config: ArrConfig | None = None,
                 searcher: Searcher | None = None, start_worker: bool = True):
        self.tools = tools
        self.cfg = config or ArrConfig()
        self.searcher = searcher or tools.searcher
        self._secret = secret.encode()
        self._api_key = api_key if callable(api_key) else (lambda: api_key)
        self.complete_dir = self.cfg.complete_dir or os.path.join(tools.config.download_dir, "arr")
        self.queue = Queue(os.path.join(tools.config.state_dir, "arr-queue.sqlite"))
        self._cache: dict[tuple, tuple[float, list[SearchHit]]] = {}
        self._cache_lock = threading.Lock()
        self._cancel: set[str] = set()
        self._current: str | None = None
        self.last_error: str | None = None
        self.paused = False
        if start_worker:
            threading.Thread(target=self._worker, name="arr-worker", daemon=True).start()

    # ------------------------------------------------------------- search

    @staticmethod
    def queries(wanted: Wanted) -> list[str]:
        """What to type into chomikuj search. A bare series title buries the episode under hundreds of
        files, but the site matches 'S01E02' and 'odc 2' as words, so TV gets targeted queries."""
        t = wanted.title
        if wanted.is_tv and wanted.episode is not None:
            return [f"{t} S{wanted.season:02d}E{wanted.episode:02d}", f"{t} odc {wanted.episode}"]
        if wanted.is_tv:
            return [f"{t} S{wanted.season:02d}", f"{t} sezon {wanted.season}"]
        return [t]

    def _search_one(self, query: str, pages: int) -> list[SearchHit]:
        key = (query.lower(), self.cfg.min_size_mb, self.cfg.max_size_mb, pages)
        now = time.time()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self.cfg.cache_seconds:
                return cached[1]
        hits: list[SearchHit] = []
        for page in range(1, max(1, pages) + 1):
            res = self.searcher.search(query, file_type="video", size_from_mb=int(self.cfg.min_size_mb),
                                       size_to_mb=int(self.cfg.max_size_mb), page=page)
            hits += res.hits
            if not res.has_next:
                break
        with self._cache_lock:
            self._cache[key] = (now, hits)
        return hits

    def _search(self, wanted: Wanted) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for q in self.queries(wanted):
            hits += self._search_one(q, self.cfg.pages)
        return hits

    def find(self, wanted: Wanted) -> list[Release]:
        seen, out = set(), []
        min_b = int(self.cfg.min_size_mb * 1024**2)
        max_b = int(self.cfg.max_size_mb * 1024**2)
        for hit in self._search(wanted):
            r = match(hit, wanted, min_bytes=min_b, max_bytes=max_b)
            if r and hit.id not in seen:
                seen.add(hit.id)
                out.append(r)
        return out

    # --------------------------------------------------------------- auth

    def _key_ok(self, params) -> bool:
        given = params.get("apikey") or params.get("ma_password") or ""
        return bool(given) and hmac.compare_digest(given.encode(), self._api_key().encode())

    def _sign(self, payload: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=")
        mac = hmac.new(self._secret, raw.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{raw}.{mac}"

    def _unsign(self, token: str) -> dict | None:
        raw, _, mac = token.strip().partition(".")
        want = hmac.new(self._secret, raw.encode(), hashlib.sha256).hexdigest()[:32]
        if not mac or not hmac.compare_digest(mac, want):
            return None
        try:
            return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        except ValueError:
            return None

    # ------------------------------------------------------------ newznab

    def newznab(self, request):
        from starlette.responses import Response

        p = request.query_params
        t = (p.get("t") or "").lower()
        if t == "caps":
            return Response(CAPS, media_type="application/xml")
        if not self._key_ok(p):
            return _nz_error(100, "Incorrect user credentials")
        if t not in ("search", "tvsearch", "movie"):
            return _nz_error(202, f"No such function ({t})")
        q = p.get("q") or ""
        base = str(request.base_url).rstrip("/")
        if not q.strip():
            # RSS sync / connection test. Chomikuj has no "latest" feed: return
            # one item that matches nothing, so the arr's Test button passes.
            return Response(self._rss([], base, placeholder=True), media_type="application/rss+xml")
        wanted = parse_query(q, season=p.get("season"), ep=p.get("ep"), movie=(t == "movie"))
        if t == "tvsearch" and not wanted.is_tv:
            return Response(self._rss([], base), media_type="application/rss+xml")
        try:
            releases = self.find(wanted)
            self.last_error = None
        except CaptchaRequired as e:
            self.last_error = str(e)
            return _nz_error(900, "chomikuj.pl wants a captcha: open chomikuj.pl in a browser, then retry")
        except (ChomikError, OSError) as e:
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("arr search %r failed: %s", q, e)
            return _nz_error(900, f"chomikuj.pl search failed: {e}")
        offset = int(p.get("offset") or 0)
        limit = int(p.get("limit") or 100)
        return Response(self._rss(releases[offset: offset + limit], base), media_type="application/rss+xml")

    def _rss(self, releases: list[Release], base: str, placeholder: bool = False) -> str:
        items = []
        if placeholder:
            items.append(self._item("Chomikuj.Bridge.Connection.Test.S01E01.720p.WEB-DL-Chomikuj", "chomik-test",
                                    1024**3, None, base, {"id": "0", "name": "test.mkv", "owner": "", "folder_path": ""},
                                    [CAT_TV, CAT_TV_HD, CAT_MOVIES, CAT_MOVIES_HD], "https://chomikuj.pl/"))
        for r in releases:
            h = r.hit
            cats = ([CAT_TV, _tv_cat(r)] if r.season is not None else [CAT_MOVIES, _movie_cat(r)])
            payload = {"id": h.id, "name": h.name, "owner": h.owner, "folder_path": h.folder_path,
                       "size_bytes": h.size_bytes, "title": r.title}
            items.append(self._item(r.title, f"chomik-{h.id}", h.size_bytes, h.date, base, payload,
                                    sorted(set(cats)), h.url))
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<rss version="2.0" xmlns:newznab="{NEWZNAB_NS}"><channel>'
            "<title>chomikuj.pl</title><description>chomikuj.pl via chomik-mcp</description>"
            f'<newznab:response offset="0" total="{len(items)}"/>'
            + "".join(items) + "</channel></rss>"
        )

    def _item(self, title, guid, size, date, base, payload, cats, comments) -> str:
        url = f"{base}/arr/newznab/nzb/{self._sign(payload)}"
        pub = format_datetime(date.replace(tzinfo=None).astimezone() if date else _epoch())
        attrs = "".join(f'<newznab:attr name="category" value="{c}"/>' for c in cats)
        return (
            f"<item><title>{escape(title)}</title><guid isPermaLink=\"false\">{escape(guid)}</guid>"
            f"<link>{escape(url)}</link><comments>{escape(comments)}</comments><pubDate>{pub}</pubDate>"
            f"<category>{cats[0]}</category>"
            f'<enclosure url="{escape(url)}" length="{int(size)}" type="application/x-nzb"/>'
            f'<newznab:attr name="size" value="{int(size)}"/>{attrs}'
            '<newznab:attr name="grabs" value="0"/></item>'
        )

    def nzb(self, request):
        from starlette.responses import Response

        token = request.path_params["token"]
        payload = self._unsign(token)
        if payload is None:
            return Response("bad token\n", status_code=404, media_type="text/plain")
        return Response(make_nzb(payload, token), media_type="application/x-nzb", headers={
            "Content-Disposition": f'attachment; filename="{_ascii(payload.get("title") or payload["name"])}.nzb"'})

    # ------------------------------------------------------------ sabnzbd

    def sabnzbd(self, request):
        return self._sab(request)

    async def sabnzbd_post(self, request):
        from starlette.concurrency import run_in_threadpool

        form = await request.form()
        upload = form.get("name") or form.get("nzbfile")
        data = await upload.read() if hasattr(upload, "read") else None
        return await run_in_threadpool(self._sab, request, data, dict(form))

    def _sab(self, request, nzb_data: bytes | None = None, form: dict | None = None):
        from starlette.responses import JSONResponse

        p = {**request.query_params, **{k: v for k, v in (form or {}).items() if isinstance(v, str)}}
        mode = (p.get("mode") or "").lower()
        if mode == "version":
            return JSONResponse({"version": VERSION})
        if not self._key_ok(p):
            return JSONResponse({"status": False, "error": "API Key Incorrect"})
        if mode == "get_config":
            return JSONResponse({"config": self._sab_config()})
        if mode == "fullstatus":
            return JSONResponse({"status": {"completedir": self.complete_dir, "paused": self.paused}})
        if mode == "get_cats":
            return JSONResponse({"categories": ["*", *self._categories()]})
        if mode in ("addfile", "addurl", "addlocalfile"):
            return JSONResponse(self._sab_add(mode, p, nzb_data))
        if mode == "queue":
            if p.get("name") == "delete":
                return JSONResponse(self._sab_delete(p.get("value", ""), p.get("del_files") == "1"))
            if p.get("name") == "pause":
                self.paused = True
            if p.get("name") == "resume":
                self.paused = False
                self.queue.wake.set()
            return JSONResponse({"queue": self._sab_queue(p)})
        if mode == "history":
            if p.get("name") == "delete":
                return JSONResponse(self._sab_delete(p.get("value", ""), p.get("del_files") == "1"))
            return JSONResponse({"history": self._sab_history(p)})
        if mode in ("pause", "resume"):
            self.paused = mode == "pause"
            self.queue.wake.set()
            return JSONResponse({"status": True})
        if mode == "warnings":
            return JSONResponse({"warnings": [self.last_error] if self.last_error else []})
        return JSONResponse({"status": False, "error": f"not implemented: {mode}"})

    def _categories(self) -> list[str]:
        cats = {"tv", "movies", "sonarr", "radarr", "tv-sonarr", "radarr-movies"}
        if os.path.isdir(self.complete_dir):
            cats |= {d for d in os.listdir(self.complete_dir) if os.path.isdir(os.path.join(self.complete_dir, d))}
        # Like SABnzbd, keep category folders in place: the arrs' health check warns when they're missing.
        for c in cats:
            try:
                os.makedirs(os.path.join(self.complete_dir, c), exist_ok=True)
            except OSError:
                break
        return sorted(cats)

    def _sab_config(self) -> dict:
        return {
            "misc": {
                "complete_dir": self.complete_dir, "download_dir": self.complete_dir, "pre_check": False,
                "enable_tv_sorting": False, "tv_categories": [], "enable_movie_sorting": False,
                "movie_categories": [], "enable_date_sorting": False, "date_categories": [],
                "history_retention": "", "history_retention_option": "all", "history_retention_number": 0,
            },
            "categories": [{"name": "*", "order": 0, "pp": "3", "script": "None", "dir": "", "priority": 0},
                           *[{"name": c, "order": i + 1, "pp": "", "script": "Default", "dir": c, "priority": -100}
                             for i, c in enumerate(self._categories())]],
            "sorters": [],
        }

    def _sab_add(self, mode: str, p: dict, nzb_data: bytes | None) -> dict:
        payload = None
        if mode == "addurl":
            m = re.search(r"/arr/newznab/nzb/([^/?#\s]+)", p.get("name") or "")
            payload = self._unsign(m.group(1)) if m else None
        elif nzb_data:
            token = read_nzb_token(nzb_data)
            payload = self._unsign(token) if token else None
        if not payload:
            return {"status": False, "error": "not a chomik NZB (only NZBs from this server's indexer work)"}
        category = _safe(p.get("cat") or "default") or "default"
        name = p.get("nzbname") or payload.get("title") or stem_and_ext(payload["name"])[0]
        if name.lower().endswith(".nzb"):
            name = name[:-4]
        hit = {k: payload.get(k) for k in ("id", "name", "owner", "folder_path", "size_bytes")}
        hit["id"] = str(hit["id"])
        nzo_id = self.queue.add(name, category, hit)
        log.info("arr: queued %s (%s) as %s", name, payload["name"], nzo_id)
        return {"status": True, "nzo_ids": [nzo_id]}

    def _sab_delete(self, value: str, del_files: bool) -> dict:
        ids = [v.strip() for v in value.split(",") if v.strip()]
        if value in ("all", "failed", "completed"):
            pool = self.queue.history() if value != "all" else [*self.queue.active(), *self.queue.history()]
            ids = [i.nzo_id for i in pool if value == "all" or i.status.lower() == value]
        for nzo_id in ids:
            if nzo_id == self._current:
                self._cancel.add(nzo_id)
            item = self.queue.hide(nzo_id)
            if item and del_files and item.storage and nzo_id != self._current:
                self._remove_storage(item.storage)
        return {"status": True, "nzo_ids": ids}

    def _remove_storage(self, path: str) -> None:
        """Delete a download folder, but only one inside complete_dir."""
        root = os.path.realpath(self.complete_dir)
        full = os.path.realpath(path)
        if full != root and os.path.commonpath([root, full]) == root:
            shutil.rmtree(full, ignore_errors=True)

    def _sab_queue(self, p: dict) -> dict:
        slots = []
        speed = 0.0
        for i, item in enumerate(self.queue.active()):
            total = item.bytes_total or 0
            left = max(total - item.bytes_done, 0)
            rate = (item.bytes_done / max(time.time() - item.started, 1)) if item.started else 0
            if item.status == "Downloading":
                speed = rate
            eta = int(left / rate) if rate > 0 else 0
            status = "Downloading" if item.status == "Downloading" else ("Paused" if self.paused else "Queued")
            slots.append({
                "status": status, "index": i, "nzo_id": item.nzo_id, "filename": item.name, "cat": item.category,
                "priority": "Normal", "mb": f"{total / 1024**2:.2f}", "mbleft": f"{left / 1024**2:.2f}",
                "percentage": str(int(item.bytes_done * 100 / total) if total else 0),
                "timeleft": f"{eta // 3600}:{eta % 3600 // 60:02d}:{eta % 60:02d}", "size": _human(total),
                "sizeleft": _human(left),
            })
        return {"status": "Paused" if self.paused else ("Downloading" if self._current else "Idle"),
                "paused": self.paused, "speedlimit": "100", "speedlimit_abs": "", "kbpersec": f"{speed / 1024:.2f}",
                "noofslots": len(slots), "noofslots_total": len(slots), "slots": slots}

    def _sab_history(self, p: dict) -> dict:
        items = self.queue.history()
        if p.get("cat"):
            items = [i for i in items if i.category == p["cat"]]
        start = int(p.get("start") or 0)
        limit = int(p.get("limit") or 0) or len(items)
        slots = [{
            "nzo_id": i.nzo_id, "name": i.name, "nzb_name": i.name + ".nzb", "category": i.category,
            "status": i.status, "fail_message": i.fail_message, "bytes": i.bytes_done if i.status == "Completed" else 0,
            "size": _human(i.bytes_done), "storage": i.storage or "",
            "download_time": int((i.finished or 0) - (i.started or i.finished or 0)),
            "completed": int(i.finished or 0), "downloaded": i.bytes_done, "pp": "D", "script": "None",
        } for i in items[start: start + limit]]
        return {"noofslots": len(items), "slots": slots}

    # ------------------------------------------------------------- worker

    def _worker(self) -> None:
        while True:
            item = None if self.paused else self.queue.next_queued()
            if item is None:
                self.queue.wake.wait(30)
                self.queue.wake.clear()
                continue
            self._run(item)

    def _run(self, item: Item) -> None:
        self._current = item.nzo_id
        folder = os.path.join(self.complete_dir, item.category, _safe(item.name))
        self.queue.update(item.nzo_id, status="Downloading", started=item.started or time.time(), storage=folder)
        try:
            account = self.cfg.account or self.tools._default_reader()
            client = self.tools.client(account, fresh=True)
            hit = SearchHit(item.hit["id"], item.hit["name"], "", int(item.hit.get("size_bytes") or 0),
                            item.hit.get("owner", ""), item.hit["folder_path"], "")
            f = client.resolve_hit(hit)
            self._check_budget(item, f.size, f.agreement.name, client.balance)
            self.queue.update(item.nzo_id, bytes_total=f.size)
            _, ext = stem_and_ext(f.name)
            dest = os.path.join(folder, _safe(item.name) + (f".{ext}" if ext else ""))

            last = [0.0]

            def progress(done: int, total: int) -> None:
                if item.nzo_id in self._cancel:
                    raise Cancelled()
                if time.time() - last[0] > 2 or done >= total:
                    last[0] = time.time()
                    self.queue.update(item.nzo_id, bytes_done=done)

            client.download_to(f, dest, on_progress=progress)
            size = os.path.getsize(dest)
            self.queue.update(item.nzo_id, status="Completed", bytes_done=size, finished=time.time())
            log.info("arr: finished %s -> %s", item.name, dest)
        except Cancelled:
            log.info("arr: %s removed by the arr while downloading", item.name)
            self._remove_storage(folder)
        except Exception as e:  # noqa: BLE001 - reported to the arr as a failed download
            log.warning("arr: %s failed: %s", item.name, e)
            self.queue.update(item.nzo_id, status="Failed", fail_message=f"{type(e).__name__}: {e}"[:500],
                              finished=time.time())
        finally:
            self._cancel.discard(item.nzo_id)
            self._current = None

    def _check_budget(self, item: Item, size: int, agreement: str, balance: Balance | None) -> None:
        if self.cfg.max_size_mb and size > self.cfg.max_size_mb * 1024**2:
            raise ChomikError(f"file is {size / 1024**2:.0f} MB, over ARR_MAX_SIZE_MB={self.cfg.max_size_mb:g}")
        if self.cfg.monthly_limit_gb:
            used = self.queue.bytes_this_month() - item.bytes_done
            if used + size > self.cfg.monthly_limit_gb * 1024**3:
                raise ChomikError(f"monthly limit ARR_MONTHLY_LIMIT_GB={self.cfg.monthly_limit_gb:g} would be "
                                  f"exceeded ({used / 1024**3:.1f} GB used this month)")
        if agreement != "own" and balance is not None:
            need_mb = size / 1024**2 + self.cfg.transfer_reserve_mb
            if balance.transfer_available < need_mb:
                raise ChomikError(f"not enough transfer on the account: {balance.transfer_available} MB left, "
                                  f"file needs {size / 1024**2:.0f} MB (+ reserve {self.cfg.transfer_reserve_mb:g})")

    # ------------------------------------------------------------- routes

    def health(self, request):
        from starlette.responses import JSONResponse

        if not self._key_ok(request.query_params):
            return JSONResponse({"error": "apikey required"}, status_code=401)
        return JSONResponse({"queued": len(self.queue.active()), "downloading": self._current,
                             "paused": self.paused, "complete_dir": self.complete_dir, "last_error": self.last_error,
                             "month_gb": round(self.queue.bytes_this_month() / 1024**3, 2)})

    def routes(self):
        from starlette.routing import Route

        return [
            Route("/arr/health", self.health),
            Route("/arr/newznab/api", self.newznab),
            Route("/arr/newznab/nzb/{token}", self.nzb),
            Route("/arr/sabnzbd/api", self.sabnzbd, methods=["GET"]),
            Route("/arr/sabnzbd/api", self.sabnzbd_post, methods=["POST"]),
        ]


# ---------------------------------------------------------------- helpers

CAPS = f"""<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server version="1.0" title="chomikuj.pl" strapline="chomikuj.pl via chomik-mcp"/>
  <limits max="100" default="100"/>
  <registration available="no" open="no"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,season,ep"/>
    <movie-search available="yes" supportedParams="q"/>
    <audio-search available="no" supportedParams=""/>
    <book-search available="no" supportedParams=""/>
  </searching>
  <categories>
    <category id="{CAT_MOVIES}" name="Movies"><subcat id="{CAT_MOVIES_HD}" name="HD"/></category>
    <category id="{CAT_TV}" name="TV"><subcat id="{CAT_TV_HD}" name="HD"/></category>
  </categories>
</caps>
"""


def _nz_error(code: int, text: str):
    from starlette.responses import Response

    body = f'<?xml version="1.0" encoding="UTF-8"?>\n<error code="{code}" description="{escape(text, {chr(34): "&quot;"})}"/>'
    return Response(body, media_type="application/xml")


def make_nzb(payload: dict, token: str) -> str:
    """A syntactically valid NZB (Sonarr/Radarr validate it) whose meta carries the signed chomikuj file token."""
    data = escape(token)
    name = escape(payload.get("title") or payload["name"])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">\n'
        f'<nzb xmlns="{NZB_NS}"><head><meta type="name">{name}</meta>'
        f'<meta type="chomik">{data}</meta></head>'
        f'<file poster="chomik-mcp" date="{int(time.time())}" subject="{name} (1/1)">'
        "<groups><group>alt.binaries.chomikuj</group></groups>"
        f'<segments><segment bytes="{int(payload.get("size_bytes") or 1)}" number="1">'
        f'chomik-{escape(str(payload["id"]))}@chomikuj.pl</segment></segments></file></nzb>\n'
    )


def read_nzb_token(data: bytes) -> str | None:
    """The signed token from an NZB made by make_nzb (verify it with ArrBridge._unsign)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    for meta in root.iter(f"{{{NZB_NS}}}meta"):
        if meta.get("type") == "chomik" and meta.text:
            return meta.text.strip()
    return None


def _safe(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return name[:200] or "_"


def _ascii(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:150]


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


def _epoch():
    from datetime import datetime, timezone

    return datetime(2000, 1, 1, tzinfo=timezone.utc)
