"""ChomikBox SOAP client: login, folder tree, file listing, download, upload.

Merged from chomik-web-downloader and chomik-web-uploader (both verified live
2026-05/07). One client = one account. Not thread-safe; use one per worker.
"""
from __future__ import annotations

import hashlib
import html
import logging
import os
import socket
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from typing import BinaryIO, Callable, Iterable, Iterator

import requests

from . import soap
from .errors import ApiError, AuthError, DownloadError, ProtectedAccountError, ProtocolError, UploadError
from .models import Agreement, Balance, Folder, FolderListing, RemoteFile
from .names import chomik_path, remote_dirname, remote_filename

log = logging.getLogger("chomik_core")

# Version sent in Auth and in the upload form (verified by the uploader).
CLIENT_VERSION = "2.0.8.2"
# Chomikuj serves a ~14 KB HTML error page for videos unless the UA looks like
# ChomikBox. This exact string was verified by the downloader.
USER_AGENT = "Mozilla/4.0 (compatible; MSIE 8.0; Windows NT 6.1; ChomikBox/2.0.7.9)"

ProgressFn = Callable[[int, int], None]  # (done_bytes, total_bytes)


def protected_usernames() -> set[str]:
    """Lower-cased usernames that must never be deleted from (read live from env each call)."""
    names = {os.environ.get("CHOMIK_A_USER", "")}
    names.update(os.environ.get("CHOMIK_PROTECTED_USERS", "").split(","))
    return {n.strip().lower() for n in names if n.strip()}


def _notify(cb: ProgressFn | None, done: int, total: int) -> None:
    if cb is None:
        return
    try:
        cb(done, total)
    except Exception:  # never let a UI callback kill a transfer
        log.exception("progress callback failed")


class ChomikClient:
    RELOGIN_AFTER = 300  # seconds; tokens were observed to live longer, this is conservative
    SOAP_TIMEOUT = 30
    UPLOAD_SOCK_TIMEOUT = 300
    CHUNK_SIZE = 65536
    # When chomikuj withholds a download URL (account busy, another stream
    # running) wait and retry instead of dropping the file.
    URL_RETRY_DELAYS: tuple[float, ...] = (15, 60)
    # Waits between attempts when box.chomikuj.pl drops or times out a SOAP call.
    NET_RETRY_DELAYS: tuple[float, ...] = (10, 30, 90)

    def __init__(
        self,
        username: str,
        password: str | None = None,
        *,
        password_hash: str | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not (password or password_hash):
            raise ValueError("password or password_hash required")
        self.username = username
        self.password_hash = (password_hash or hashlib.md5(password.encode("utf-8")).hexdigest()).lower()
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "pl-PL,en,*"})
        self._sleep = sleep
        self.token: str | None = None
        self.hamster_id: str | None = None
        self.balance: Balance | None = None
        self._login_at = 0.0
        self._stamp = 0
        self._children_cache: dict[str, list[Folder]] = {}

    @property
    def protected(self) -> bool:
        """True when this account must never lose data (no delete/move-out/overwrite).

        Always on for the CHOMIK_A_USER account and anything in CHOMIK_PROTECTED_USERS.
        Not switchable off from code on purpose.
        """
        return self.username.lower() in protected_usernames()

    def assert_deletable(self, what: str = "delete") -> None:
        """Every destructive operation must call this first."""
        if self.protected:
            raise ProtectedAccountError(f"refusing to {what} on protected account {self.username!r}")

    @classmethod
    def from_env(cls, prefix: str = "CHOMIK_", **kw) -> "ChomikClient":
        """Build from {prefix}USER plus {prefix}PASSWORD or {prefix}PASSWORD_HASH."""
        user = os.environ.get(prefix + "USER")
        if not user:
            raise ValueError(f"{prefix}USER not set")
        return cls(
            user,
            os.environ.get(prefix + "PASSWORD"),
            password_hash=os.environ.get(prefix + "PASSWORD_HASH"),
            **kw,
        )

    # ------------------------------------------------------------------ SOAP

    def _post(self, action: str, inner_xml: str) -> ET.Element:
        """POST one SOAP call, return the <{action}Result> element."""
        body = soap.envelope(action, inner_xml).encode("utf-8")
        delays = self.NET_RETRY_DELAYS
        for attempt in range(len(delays) + 1):
            try:
                r = self.session.post(soap.SOAP_URL, data=body, headers=soap.headers(action), timeout=self.SOAP_TIMEOUT)
                break
            except requests.RequestException as e:
                if attempt == len(delays):
                    raise ProtocolError(f"{action}: request failed: {e}") from e
                log.warning("%s: network error (%s); retry %d in %ss", action, e, attempt + 1, delays[attempt])
                self._sleep(delays[attempt])
        root = soap.parse(r.text)
        result = soap.find(root, action + "Result")
        if result is None:
            fault = soap.find(root, "faultstring")
            raise ProtocolError(f"{action}: no {action}Result (HTTP {r.status_code}, fault={getattr(fault, 'text', None)!r})")
        seq = soap.child(result, "messageSequence")
        server_stamp = soap.text(seq, "stamp")
        if server_stamp and server_stamp.isdigit():
            self._stamp = max(self._stamp, int(server_stamp))
        return result

    def _call(self, action: str, inner_xml: str) -> ET.Element:
        result = self._post(action, inner_xml)
        status = (soap.text(result, "status") or "").lower()
        if status != "ok":
            raise ApiError(action, soap.text(result, "errorMessage") or status or None)
        return result

    def _next_stamp(self) -> int:
        self._stamp += 1
        return self._stamp

    # ----------------------------------------------------------------- Auth

    def login(self, force: bool = False) -> None:
        if not force and self.token and time.time() < self._login_at + self.RELOGIN_AFTER:
            return
        inner = (
            f"<name>{soap.esc(self.username)}</name>"
            f"<passHash>{self.password_hash}</passHash>"
            "<ver>4</ver>"
            f"<client><name>chomikbox</name><version>{CLIENT_VERSION}</version></client>"
        )
        try:
            result = self._call("Auth", inner)
        except ApiError as e:
            raise AuthError(f"login rejected for {self.username!r}: {e.code}") from e
        token = soap.text(result, "token")
        hamster = soap.text(result, "hamsterId")
        if not token or token == "-1" or not hamster or hamster == "-1":
            raise AuthError(f"login for {self.username!r} returned no token")
        self.token, self.hamster_id = token, hamster
        self._login_at = time.time()
        self._children_cache.clear()
        log.info("logged in as %s (hamsterId=%s)", self.username, hamster)

    # -------------------------------------------------------------- Folders

    def list_folders(self, folder_id: str = "0", parent_path: tuple[str, ...] = (), *, refresh: bool = False) -> list[Folder]:
        """Direct subfolders of `folder_id` in this account ("0" = root)."""
        fid = str(folder_id)
        if not refresh and fid in self._children_cache:
            return self._children_cache[fid]
        self.login()
        inner = (
            f"<token>{self.token}</token>"
            f"<hamsterId>{self.hamster_id}</hamsterId>"
            f"<folderId>{fid}</folderId>"
            "<depth>0</depth>"
        )
        result = self._call("Folders", inner)
        folders = parse_folders(result, parent_path)
        self._children_cache[fid] = folders
        return folders

    def walk(self, folder_id: str = "0", path: tuple[str, ...] = ()) -> Iterator[Folder]:
        """Depth-first walk of every folder below `folder_id` (not including it)."""
        for f in self.list_folders(folder_id, path):
            yield f
            yield from self.walk(f.id, f.path)

    def find_folder(self, path: str | Iterable[str]) -> Folder | None:
        parts = _split(path)
        current = Folder("0", "", ())
        for part in parts:
            want = remote_dirname(part)
            match = next((f for f in self.list_folders(current.id, current.path) if f.name == want), None)
            if match is None:
                return None
            current = match
        return current

    def mkdir_p(self, path: str | Iterable[str]) -> Folder:
        """Return the folder at `path`, creating missing parts."""
        current = Folder("0", "", ())
        for part in _split(path):
            want = remote_dirname(part)
            if not want:
                continue
            match = next((f for f in self.list_folders(current.id, current.path) if f.name == want), None)
            if match is None:
                self._add_folder(want, current.id)
                match = next(
                    (f for f in self.list_folders(current.id, current.path, refresh=True) if f.name == want), None
                )
                if match is None:
                    raise ProtocolError(f"AddFolder said ok but {want!r} not listed under folder {current.id}")
            current = match
        return current

    def _add_folder(self, name: str, parent_id: str) -> None:
        self.login()
        inner = f"<token>{self.token}</token><newFolderId>{parent_id}</newFolderId><name>{soap.esc(name)}</name>"
        try:
            self._call("AddFolder", inner)
        except ApiError as e:
            if "NameExistsAtDestination" not in e.code:
                raise
        self._children_cache.pop(str(parent_id), None)

    # --------------------------------------------------------------- Files

    NOT_AVAILABLE = "requested file(s) not available"

    def list_files(self, path: str, *, missing_ok: bool = False) -> FolderListing:
        """Files directly inside a folder given by chomikuj path ('/user/Folder+Name').

        Works for this account and for other users' public folders (it uses
        this account's token). Does not recurse.

        The server answers "requested file(s) not available" for folders with
        no files (seen live on fresh accounts) and for paths that don't exist;
        `missing_ok=True` turns that into an empty listing.
        """
        self.login()
        inner = self._download_inner(f"<DownloadReqEntry><id>{soap.esc(path)}</id></DownloadReqEntry>")
        try:
            result = self._call("Download", inner)
        except ApiError as e:
            if missing_ok and self.NOT_AVAILABLE in e.code:
                return FolderListing(path=path, owner="", files=[], balance=self.balance)
            raise
        self.balance = parse_balance(result) or self.balance
        listings = parse_download(result)
        if not listings:
            return FolderListing(path=path, owner="", files=[], balance=self.balance)
        if len(listings) > 1:
            log.warning("list_files(%s): %d folders in response, merging", path, len(listings))
        first = listings[0]
        for extra in listings[1:]:
            first.files.extend(extra.files)
        first.balance = self.balance
        return first

    def list_files_in(self, folder: Folder, *, missing_ok: bool = False) -> FolderListing:
        """Files in one of this account's folders (from list_folders/walk)."""
        return self.list_files(chomik_path(self.username, folder.path), missing_ok=missing_ok)

    def resolve_download(self, f: RemoteFile) -> RemoteFile:
        """Fill `f.url` with a signed download URL. Retries when chomikuj withholds it."""
        delays = (0.0, *self.URL_RETRY_DELAYS)
        for attempt, delay in enumerate(delays, 1):
            if delay:
                log.info("no URL for %s yet, retry %d in %ss", f.name, attempt, delay)
                self._sleep(delay)
            self.login()
            cost = "" if f.agreement.name == "small" else f"<cost>{f.agreement.cost}</cost>"
            entry = (
                f"<DownloadReqEntry><id>{f.id}</id>"
                f"<agreementInfo><AgreementInfo><name>{soap.esc(f.agreement.name)}</name>{cost}</AgreementInfo></agreementInfo>"
                "</DownloadReqEntry>"
            )
            result = self._call("Download", self._download_inner(entry))
            self.balance = parse_balance(result) or self.balance
            for listing in parse_download(result):
                for got in listing.files:
                    if got.id == f.id and got.url:
                        f.url = got.url
                        return f
        raise DownloadError(f"chomikuj withheld the download URL for {f.name!r} after {len(delays)} attempts")

    def resolve_hit(self, hit) -> RemoteFile:
        """Turn a search.SearchHit (or anything with id, name, folder_path) into a downloadable RemoteFile.

        Lists the containing folder to get the exact size and the agreement.
        Falls back to matching by name, since the SOAP listing was seen to
        renumber or hide duplicates.
        """
        listing = self.list_files(hit.folder_path, missing_ok=True)
        match = next((f for f in listing.files if f.id == str(hit.id)), None)
        match = match or next((f for f in listing.files if f.name == hit.name), None)
        if match is None:
            raise DownloadError(f"{hit.name!r} is no longer listed in {hit.folder_path} "
                                f"(deleted, moved, or the folder needs a password)")
        return match

    def _download_inner(self, entries: str) -> str:
        return (
            f"<token>{self.token}</token>"
            f"<sequence><stamp>{self._next_stamp()}</stamp><part>0</part><count>1</count></sequence>"
            "<disposition>download</disposition>"
            f"<list>{entries}</list>"
        )

    @contextmanager
    def open_download(self, f: RemoteFile, offset: int = 0) -> Iterator["DownloadStream"]:
        """Open the file's byte stream. Resolves the URL if needed. Use as a context manager."""
        if not f.url:
            self.resolve_download(f)
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            r = self.session.get(f.url, headers=headers, stream=True, timeout=(30, self.UPLOAD_SOCK_TIMEOUT))
        except requests.RequestException as e:
            raise DownloadError(f"{f.name}: {e}") from e
        try:
            if r.status_code == 404:
                raise DownloadError(f"{f.name}: 404 Not Found")
            if r.status_code >= 400:
                raise DownloadError(f"{f.name}: HTTP {r.status_code}")
            if "text/html" in (r.headers.get("content-type") or "").lower():
                # Fake "success" page: UA blocked, quota exhausted or login lost.
                snippet = next(r.iter_content(4096), b"").decode("utf-8", "replace")[:200]
                f.url = None  # signed URL is spent / useless
                raise DownloadError(f"{f.name}: server returned HTML instead of the file: {snippet!r}")
            length = r.headers.get("content-length")
            remaining = int(length) if length and length.isdigit() else max(f.size - offset, 0)
            yield DownloadStream(r, remaining, self.CHUNK_SIZE)
        finally:
            r.close()

    def download_to(self, f: RemoteFile, dest: str, on_progress: ProgressFn | None = None) -> str:
        """Download to `dest`, resuming from `dest + '.part'` if present. Returns `dest`."""
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        part = dest + ".part"
        offset = os.path.getsize(part) if os.path.exists(part) else 0
        if f.size and offset >= f.size:
            os.replace(part, dest)
            return dest
        written = offset
        with self.open_download(f, offset) as stream, open(part, "ab" if offset else "wb") as out:
            _notify(on_progress, written, f.size)
            for chunk in stream:
                out.write(chunk)
                written += len(chunk)
                _notify(on_progress, written, f.size)
        # Truncated-stream guard from the downloader (<50% = failure).
        if f.size and written < f.size * 0.5:
            raise DownloadError(f"{f.name}: only got {written} of {f.size} bytes")
        if f.size and written != f.size:
            log.warning("%s: size mismatch, got %d expected %d", f.name, written, f.size)
        os.replace(part, dest)
        return dest

    # --------------------------------------------------------------- Upload

    def upload(
        self,
        source: BinaryIO | Iterable[bytes],
        size: int,
        name: str,
        folder_id: str,
        on_progress: ProgressFn | None = None,
    ) -> str:
        """Upload exactly `size` bytes from a file object or byte iterator. Returns the stored name."""
        self.login()
        name = remote_filename(name)
        inner = f"<token>{self.token}</token><folderId>{folder_id}</folderId><fileName>{soap.esc(name)}</fileName>"
        try:
            result = self._call("UploadToken", inner)
        except ApiError as e:
            raise UploadError(f"UploadToken rejected for {name!r}: {e.code}") from e
        key, stamp, server = (soap.text(result, k) for k in ("key", "stamp", "server"))
        if not (key and stamp and server):
            raise UploadError("UploadToken response missing key/stamp/server")
        host, _, port = server.partition(":")
        port = port or "80"

        head, tail = build_upload_request(host, port, key, stamp, name, size, self.hamster_id, folder_id)
        chunks = _iter_chunks(source, self.CHUNK_SIZE)
        sent = 0
        try:
            with socket.create_connection((host, int(port)), timeout=self.UPLOAD_SOCK_TIMEOUT) as sock:
                sock.sendall(head)
                _notify(on_progress, 0, size)
                for chunk in chunks:
                    if sent + len(chunk) > size:
                        raise UploadError(f"{name}: source produced more than the declared {size} bytes")
                    sock.sendall(chunk)
                    sent += len(chunk)
                    _notify(on_progress, sent, size)
                if sent != size:
                    raise UploadError(f"{name}: source ended at {sent} of declared {size} bytes")
                sock.sendall(tail)
                resp = b""
                while True:
                    data = sock.recv(4096)
                    if not data:
                        break
                    resp += data
                    if b"/>" in resp or b"</" in resp:
                        break
        except OSError as e:
            raise UploadError(f"{name}: socket error during upload: {e}") from e
        if b'res="1"' not in resp and b"res='1'" not in resp:
            raise UploadError(f"{name}: upload server rejected file: {resp.decode('utf-8', 'replace')[-300:]}")
        self._children_cache.pop(str(folder_id), None)
        return name

    def upload_file(
        self,
        local_path: str,
        dest: str | Iterable[str] | Folder,
        name: str | None = None,
        on_progress: ProgressFn | None = None,
    ) -> str:
        """Upload a local file into folder `dest` (path, created if missing, or a Folder)."""
        folder = dest if isinstance(dest, Folder) else self.mkdir_p(dest)
        size = os.path.getsize(local_path)
        with open(local_path, "rb") as fh:
            return self.upload(fh, size, name or os.path.basename(local_path), folder.id, on_progress)


class DownloadStream:
    """Iterable of byte chunks for an open download. `size` = bytes this stream will yield."""

    def __init__(self, response: requests.Response, size: int, chunk_size: int):
        self.response = response
        self.size = size
        self._chunk_size = chunk_size

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.response.iter_content(self._chunk_size):
            if chunk:
                yield chunk


# ------------------------------------------------------------------ parsing


def parse_folders(result: ET.Element, parent_path: tuple[str, ...] = ()) -> list[Folder]:
    folder = soap.child(result, "folder")
    container = soap.child(folder, "folders")
    out = []
    for fi in soap.children(container, "FolderInfo"):
        fid, name = soap.text(fi, "id"), soap.text(fi, "name")
        if fid and name is not None:
            # Server can return entity-escaped names even after XML parsing
            # (the uploader unescaped them a second time; kept for parity).
            name = html.unescape(name)
            out.append(Folder(fid, name, (*parent_path, name)))
    return out


def parse_balance(result: ET.Element) -> Balance | None:
    bal = soap.child(result, "accountBalance")
    if bal is None:
        return None
    tr = soap.child(bal, "transfer")

    def num(parent, key):
        v = soap.text(parent, key, "0") or "0"
        return int(v) if v.lstrip("-").isdigit() else 0

    return Balance(num(tr, "value"), num(tr, "limit"), num(tr, "extra"), num(bal, "points"))


def parse_download(result: ET.Element) -> list[FolderListing]:
    out = []
    for df in soap.children(soap.child(result, "list"), "DownloadFolder"):
        owner = soap.text(df, "hamsterName", "") or ""
        gpath = soap.text(df, "globalId", "") or ""
        listing = FolderListing(path=gpath, owner=owner)
        for fe in soap.children(soap.child(df, "files"), "FileEntry"):
            agreement = Agreement("own")
            for ai in soap.children(soap.child(fe, "agreementInfo"), "AgreementInfo"):
                if not soap.is_nil(ai):
                    cost = soap.text(ai, "cost", "0") or "0"
                    agreement = Agreement(soap.text(ai, "name", "own") or "own", int(cost) if cost.isdigit() else 0)
                    break
            size = soap.text(fe, "size", "0") or "0"
            listing.files.append(
                RemoteFile(
                    id=soap.text(fe, "id", "") or "",
                    name=soap.text(fe, "name", "") or "",
                    size=int(size) if size.isdigit() else 0,
                    agreement=agreement,
                    folder_path=gpath,
                    owner=owner,
                    url=soap.text(fe, "url"),  # ET already unescapes &amp;
                )
            )
        out.append(listing)
    return out


def build_upload_request(host, port, key, stamp, filename, size, chomik_id, folder_id) -> tuple[bytes, bytes]:
    """Raw HTTP/1.0 multipart head + tail. Byte-for-byte the format ChomikBox sends."""
    boundary = "--!CHB" + stamp
    fields = [
        ("chomik_id", chomik_id),
        ("folder_id", folder_id),
        ("key", key),
        ("time", stamp),
        ("client", "ChomikBox-" + CLIENT_VERSION),
        ("locale", "PL"),
    ]
    body_head = "".join(f'{boundary}\r\nname="{k}"\r\nContent-Type: text/plain\r\n\r\n{v}\r\n' for k, v in fields)
    safe_name = filename.replace("\\", "\\\\").replace('"', '\\"')
    body_head += f'{boundary}\r\nname="file"; filename="{safe_name}"\r\n\r\n'
    tail = "\r\n" + boundary + "--\r\n\r\n"
    head_b, tail_b = body_head.encode("utf-8"), tail.encode("utf-8")
    # Content-Length must count bytes, not characters (non-ASCII filenames).
    length = len(head_b) + size + len(tail_b)
    http = (
        "POST /file/ HTTP/1.0\r\n"
        f"Content-Type: multipart/mixed; boundary={boundary[2:]}\r\n"
        f"Host: {host}:{port}\r\n"
        f"Content-Length: {length}\r\n\r\n"
    )
    return http.encode("utf-8") + head_b, tail_b


def _iter_chunks(source: BinaryIO | Iterable[bytes], chunk_size: int) -> Iterator[bytes]:
    read = getattr(source, "read", None)
    if read is None:
        yield from source
        return
    while True:
        chunk = read(chunk_size)
        if not chunk:
            return
        yield chunk


def _split(path: str | Iterable[str]) -> list[str]:
    if isinstance(path, str):
        return [p for p in path.strip().strip("/").split("/") if p]
    return [p for p in path if p]
