"""Plan and run a clone. Copies only; never deletes, renames or moves anything anywhere."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from chomik_core import Agreement, ChomikClient, ChomikError, Folder, RemoteFile, chomik_path, relay_file, walk_public
from chomik_core.names import remote_dirname, remote_filename
from chomik_core.web import CaptchaRequired

from .state import State

log = logging.getLogger("chomik_cloner")


@dataclass
class Item:
    file: RemoteFile
    rel: tuple[str, ...]  # folder names relative to the source root
    src_folder: str | None = None  # source folder id (to check it isn't locked for the destination)


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)
    folders: int = 0
    list_errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(i.file.size for i in self.items)


# ------------------------------------------------------------------ sources


# Folder names never worth copying: Synology NAS thumbnail/index folders.
DEFAULT_SKIP_DIRS = ("@eaDir",)


def _skipped(parts, skip_dirs) -> bool:
    return any(p in skip_dirs for p in parts)


_SIZE_TEXT = re.compile(r"([\d.,]+)\s*([KMGT]?B)", re.I)
_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def size_from_text(text: str) -> int:
    """'14,4 MB' -> bytes (approximate: the website rounds sizes)."""
    m = _SIZE_TEXT.search(text or "")
    if not m:
        return 0
    return int(float(m.group(1).replace(",", ".")) * _UNITS[m.group(2).upper()])


def account_items(src: ChomikClient, only: str | None = None, skip_dirs=(), web_files=None
                  ) -> Iterator[tuple[tuple[str, ...], list[RemoteFile], str | None]]:
    """Yield (rel_path, files, error) for every folder of `src` (optionally below `only`).

    Folders named in `skip_dirs` (and everything below them) aren't listed at all.
    `web_files(folder_id)` (the website listing) adds files the SOAP listing
    leaves out: it returns only one of several identical files in a folder
    (seen live: "X.pdf", "X(1).pdf" hidden, only "X(2).pdf" listed).
    """
    if only:
        start = src.find_folder(only)
        if start is None:
            raise ChomikError(f"source folder {only!r} not found in {src.username}")
        base = start.path
        folders: list[Folder] = [start, *src.walk(start.id, start.path)]
    else:
        base = ()
        folders = [Folder("0", "", ()), *src.walk()]
    for f in folders:
        rel = f.path[len(base):]
        if _skipped(rel, skip_dirs):
            continue
        path = chomik_path(src.username, f.path)
        try:
            files = src.list_files(path, missing_ok=True).files
        except ChomikError as e:
            yield rel, [], str(e), f.id
            continue
        if web_files is not None:
            files = files + _web_only(src, f, path, files, web_files)
        yield rel, files, None, f.id


def _web_only(src, folder, path, files, web_files) -> list[RemoteFile]:
    try:
        listed = web_files(folder.id)
    except ChomikError as e:  # locked folders, network: the SOAP files are still fine
        log.debug("no website listing for %s: %s", path, e)
        return []
    known = {x.id for x in files}
    extra = [RemoteFile(w.id, w.name, size_from_text(w.size_text), Agreement("own"), folder_path=path, owner=src.username)
             for w in listed if w.id not in known]
    if extra:
        log.info("%s: %d file(s) only on the website listing: %s", path, len(extra), ", ".join(x.name for x in extra[:3]))
    return extra


def public_items(client: ChomikClient, url: str):
    for rel, listing in walk_public(client, url):
        yield rel, listing.files, None


def build_plan(source, *, max_files: int | None = None, max_size: int | None = None, skip_dirs=()) -> Plan:
    plan = Plan()
    for rel, files, err, *extra in source:
        if _skipped(rel, skip_dirs):
            continue
        src_folder = extra[0] if extra else None
        plan.folders += 1
        if err:
            plan.list_errors.append(("/".join(rel) or "/", err))
            log.warning("cannot list %s: %s", "/".join(rel) or "/", err)
        for f in files:
            if max_size and f.size > max_size:
                continue
            plan.items.append(Item(f, rel, src_folder))
            if max_files and len(plan.items) >= max_files:
                return plan
        if plan.folders % 25 == 0:
            log.info("planning: %d folders, %d files, %.1f GB so far", plan.folders, len(plan.items), plan.total_bytes / 1e9)
    return plan


# ---------------------------------------------------------------- execution


@dataclass
class Result:
    copied: int = 0  # verified present in the destination
    copied_bytes: int = 0
    skipped: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    missing: list[tuple[str, int]] = field(default_factory=list)  # site said ok, never appeared
    locked: list[tuple[str, int]] = field(default_factory=list)  # (folder, files) source locked for the destination
    stopped: str | None = None  # set when the run had to stop early (e.g. captcha)


# copier(src, file, dst, dest_folder) -> stored name. Default: stream through this machine.
Copier = Callable[[ChomikClient, RemoteFile, ChomikClient, Folder], str]
# lister(dest_folder) -> names of files currently in it.
Lister = Callable[[Folder], set]


def stream_copier(src: ChomikClient, f: RemoteFile, dst: ChomikClient, folder: Folder) -> str:
    return relay_file(src, f, dst, folder)


def server_copier(web, relogin: Callable[[], None] | None = None) -> Copier:
    """Server-side "Zachomikuj" through `web` (a ChomikWeb logged in as the destination).

    No bytes pass through this machine. The site can answer "ok" without copying
    (seen for an 11 GB file), so run() verifies arrival afterwards. If the web
    session has expired (HTML instead of JSON), `relogin` is called and the copy
    retried once.
    """
    from chomik_core.web import WebCopyError

    def copy(src, f, dst, folder):
        try:
            web.copy_file(f.id, folder.id)
        except WebCopyError as e:
            if relogin is None or "expected JSON" not in str(e):
                raise
            log.warning("web session looks expired (%s); logging in again", str(e)[:80])
            relogin()
            web.copy_file(f.id, folder.id)
        return f.name

    return copy


def soap_lister(dst: ChomikClient) -> Lister:
    """SOAP Download listing. Was seen to omit files; prefer web_lister when available."""
    return lambda folder: {x.name for x in dst.list_files(chomik_path(dst.username, folder.path), missing_ok=True).files}


# folder_copier(src_folder_id, dest_parent) copies one source folder's files into a new
# folder of the same name inside dest_parent (no subfolders).
FolderCopier = Callable[[str, Folder], None]


def server_folder_copier(web, owner: str) -> FolderCopier:
    return lambda src_folder_id, parent: web.copy_folder(owner, src_folder_id, parent.id)


def web_lister(web, account: str) -> Lister:
    """Website listing (all pages). Matched the real folder contents exactly in live tests."""
    return lambda folder: {x.name for x in web.list_files(account, folder.id)}


def run(
    plan: Plan,
    src: ChomikClient,
    dst: ChomikClient,
    dest_root: tuple[str, ...],
    state: State,
    *,
    copier: Copier = stream_copier,
    lister: Lister | None = None,
    fallback: Copier | None = None,
    verify_delays: tuple[float, ...] = (5, 20, 40),
    retries: int = 1,
    pause: float = 0.0,
    source_visible: Callable[[str], bool] | None = None,
    folder_copier: FolderCopier | None = None,
    on_file: Callable[[int, int, Item, str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    """Copy every planned file, then verify per destination folder that it arrived.

    A file is "done" only once the destination listing shows it. Files still
    absent after `verify_delays` are retried with `fallback` (e.g. streaming) if
    given, else recorded as "missing" and retried on the next run.

    With `folder_copier`, a source folder the destination doesn't have yet is
    copied with one request; the files that don't show up are then copied one
    by one as usual. Existing destination folders always go file by file (a
    folder copy into them would make a "Name(1)" duplicate).
    """
    if dst.username.lower() == src.username.lower():
        raise ChomikError("source and destination are the same account")
    lister = lister or soap_lister(dst)
    res = Result()
    total = len(plan.items)
    owner = lambda f: f.owner or src.username  # noqa: E731

    groups: list[tuple[tuple[str, ...], list[tuple[int, Item]]]] = []
    for n, item in enumerate(plan.items, 1):
        parts = (*dest_root, *[remote_dirname(p) for p in item.rel])
        if not groups or groups[-1][0] != parts:
            groups.append((parts, []))
        groups[-1][1].append((n, item))

    for parts, entries in groups:
        label = "/" + "/".join(parts)
        todo = [(n, it) for n, it in entries if state.status(owner(it.file), it.file.id, dst.username) not in ("done", "skipped_exists")]
        res.skipped += len(entries) - len(todo)
        if not todo:
            continue
        src_folder = todo[0][1].src_folder
        if source_visible is not None and src_folder is not None and not source_visible(src_folder):
            for n, it in todo:
                state.record(owner(it.file), it.file.id, dst.username, label, it.file.name, it.file.size, "locked",
                             "source folder is password-protected for the destination")
            res.locked.append((label, len(todo)))
            log.warning("source folder for %s is locked (%d files); add its password to retry", label, len(todo))
            continue
        folder = present = None
        if folder_copier is not None and _folder_copy_ok(entries, todo, parts):
            try:
                if pause and res.copied:
                    sleep(pause)
                folder, present, todo = _bulk(todo, total, parts, label, src_folder, dst, lister, folder_copier,
                                              verify_delays, sleep, state, owner, res, on_file)
            except CaptchaRequired as e:
                res.stopped = f"captcha required copying folder {label}: {e}"
                break
            except _FolderVanished as e:
                for n, it in todo:
                    state.record(owner(it.file), it.file.id, dst.username, label, it.file.name, it.file.size, "failed", str(e))
                    res.failed.append((f"{label.rstrip('/')}/{it.file.name}", str(e)))
                log.error("%s", e)
                continue
            if not todo:
                continue
        try:
            if folder is None:
                folder = dst.mkdir_p(list(parts))
                present = lister(folder)
        except CaptchaRequired as e:
            res.stopped = f"captcha required listing {label}: {e}"
            break
        except ChomikError as e:
            for n, it in todo:
                state.record(owner(it.file), it.file.id, dst.username, label, it.file.name, it.file.size, "failed", str(e))
                res.failed.append((f"{label.rstrip('/')}/{it.file.name}", str(e)))
            log.error("cannot prepare %s: %s", label, e)
            continue

        sent: list[tuple[int, Item]] = []
        for n, it in todo:
            f = it.file
            if f.name in present or remote_filename(f.name) in present:
                state.record(owner(f), f.id, dst.username, label, f.name, f.size, "skipped_exists")
                res.skipped += 1
                if on_file:
                    on_file(n, total, it, "exists")
                continue
            try:
                if pause and (sent or res.copied):
                    sleep(pause)
                copier(src, f, dst, folder)
                state.record(owner(f), f.id, dst.username, label, f.name, f.size, "copied")
                sent.append((n, it))
                if on_file:
                    on_file(n, total, it, f"sent ({f.size / 1e6:.1f} MB)")
            except CaptchaRequired as e:
                res.stopped = f"captcha required at {label}/{f.name}: {e}"
                break
            except ChomikError as e:
                state.record(owner(f), f.id, dst.username, label, f.name, f.size, "failed", str(e))
                res.failed.append((f"{label.rstrip('/')}/{f.name}", str(e)))
                log.error("failed %s/%s: %s", label, f.name, e)
                if on_file:
                    on_file(n, total, it, f"FAILED: {e}")

        _verify(sent, folder, label, lister, verify_delays, sleep, state, dst, owner, res, fallback, src, on_file, total,
                copier=copier, retries=retries if verify_delays else 0)
        if res.stopped:
            log.error("%s; stopping (rerun later resumes)", res.stopped)
            break
    return res


class _FolderVanished(ChomikError):
    pass


def _folder_copy_ok(entries, todo, parts) -> bool:
    """Only whole, untouched, non-root folders whose name the site keeps as is."""
    first = entries[0][1]
    return (len(todo) == len(entries) and bool(first.rel) and first.src_folder is not None
            and remote_dirname(first.rel[-1]) == first.rel[-1] == parts[-1])


def _bulk(todo, total, parts, label, src_folder, dst, lister, folder_copier, delays, sleep, state, owner, res, on_file):
    """Folder copy if the destination lacks the folder. Returns (folder, present, still_todo).

    (None, None, todo) when the folder already exists or the copy was refused,
    so the caller goes file by file.
    """
    parent = dst.mkdir_p(list(parts[:-1]))
    name = parts[-1]
    if any(f.name == name for f in dst.list_folders(parent.id, parent.path, refresh=True)):
        return None, None, todo
    try:
        folder_copier(src_folder, parent)
    except CaptchaRequired:
        raise
    except ChomikError as e:
        log.warning("folder copy of %s refused (%s); copying file by file", label, e)
        return None, None, todo
    folder, present = None, set()
    want = {it.file.name for _, it in todo}
    for delay in (0, *delays):
        if delay:
            sleep(delay)
        if folder is None:
            folder = next((f for f in dst.list_folders(parent.id, parent.path, refresh=True) if f.name == name), None)
            if folder is None:
                continue
        try:
            present = lister(folder)
        except ChomikError as e:
            log.warning("listing new folder %s failed: %s", label, e)
            continue
        if want <= present:
            break
    if folder is None:
        # Creating it ourselves now could leave the site's copy as "Name(1)" later; next run retries.
        raise _FolderVanished(f"folder copy of {label} said ok but the folder never appeared")
    still = []
    for n, it in todo:
        f = it.file
        if f.name in present or remote_filename(f.name) in present:
            state.record(owner(f), f.id, dst.username, label, f.name, f.size, "done")
            res.copied += 1
            res.copied_bytes += f.size
            if on_file:
                on_file(n, total, it, "copied with its folder")
        else:
            still.append((n, it))
    log.info("folder copy %s: %d of %d files arrived; %d left for file-by-file", label, len(todo) - len(still), len(todo), len(still))
    return folder, present, still


def _verify(sent, folder, label, lister, delays, sleep, state, dst, owner, res, fallback, src, on_file, total,
            copier=None, retries=0):
    waiting = list(sent)
    for attempt in range(retries + 1):
        if attempt:
            if not waiting:
                break
            # Copies were seen to fail silently and then work on a retry: send again once.
            for n, it in waiting:
                try:
                    copier(src, it.file, dst, folder)
                    if on_file:
                        on_file(n, total, it, "not there yet; sent again")
                except ChomikError as e:
                    log.warning("re-send of %s failed: %s", it.file.name, e)
        waiting = _await(waiting, folder, label, lister, delays, sleep, state, dst, owner, res)
    _finish(waiting, folder, label, delays, state, dst, owner, res, fallback, src, on_file, total)


def _await(waiting, folder, label, lister, delays, sleep, state, dst, owner, res):
    for delay in delays:
        if not waiting:
            break
        sleep(delay)
        try:
            present = lister(folder)
        except ChomikError as e:
            log.warning("verify listing of %s failed: %s", label, e)
            continue
        still = []
        for n, it in waiting:
            f = it.file
            if f.name in present or remote_filename(f.name) in present:
                state.record(owner(f), f.id, dst.username, label, f.name, f.size, "done")
                res.copied += 1
                res.copied_bytes += f.size
            else:
                still.append((n, it))
        waiting = still
    return waiting


def _finish(waiting, folder, label, delays, state, dst, owner, res, fallback, src, on_file, total):
    if not delays:  # verification disabled: trust the copier
        for n, it in waiting:
            state.record(owner(it.file), it.file.id, dst.username, label, it.file.name, it.file.size, "done")
            res.copied += 1
            res.copied_bytes += it.file.size
        return
    for n, it in waiting:
        f = it.file
        if fallback is not None:
            try:
                fallback(src, f, dst, folder)
                state.record(owner(f), f.id, dst.username, label, f.name, f.size, "done")
                res.copied += 1
                res.copied_bytes += f.size
                if on_file:
                    on_file(n, total, it, "never arrived; streamed instead")
                continue
            except ChomikError as e:
                log.error("fallback failed for %s/%s: %s", label, f.name, e)
        state.record(owner(f), f.id, dst.username, label, f.name, f.size, "missing", "site said ok, file never appeared")
        res.missing.append((f"{label.rstrip('/')}/{f.name}", f.size))
        if on_file:
            on_file(n, total, it, "MISSING: site said ok but file never appeared")
