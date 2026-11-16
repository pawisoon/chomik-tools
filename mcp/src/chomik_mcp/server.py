"""The MCP tools.

Accounts are referred to by letter (A, B, ...) or username; their logins come
from CHOMIK_<X>_USER / _PASSWORD in the environment. Profile and folder
passwords come from the passwords file. No tool deletes, moves or renames
anything on chomikuj.pl.
"""
from __future__ import annotations

import os
import sys
import threading

from chomik_core import ChomikClient, Folder, chomik_path, path_from_url, split_path, walk_public
from chomik_core.passwords import Passwords
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .config import Config
from .jobs import Job, Jobs

INSTRUCTIONS = """\
Tools for chomikuj.pl accounts configured on this server.
- Refer to accounts by letter (see `accounts`) or username. A path is "A:/Folder/Sub" or a chomikuj.pl URL.
- download, upload, clone_* start background jobs; poll them with job_status.
- Nothing is ever deleted on chomikuj.pl. Passwords are read from the server's config, never passed in.
- Run clone_account / clone_public with dry_run=true first to see what will be copied.
"""

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def _inside(base: str, *parts: str) -> str:
    """Join and make sure the result stays inside `base`."""
    root = os.path.realpath(base)
    full = os.path.realpath(os.path.join(root, *parts))
    if os.path.commonpath([root, full]) != root:
        raise ValueError(f"path escapes {base}")
    return full


def _safe_name(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").strip()
    return "_" if name in ("", ".", "..") else name


class Tools:
    def __init__(self, config: Config | None = None, client_factory=None):
        self.config = config or Config()
        self.jobs = Jobs(os.path.join(self.config.state_dir, "jobs"))
        self._client_factory = client_factory or (lambda letter: ChomikClient.from_env(f"CHOMIK_{letter}_"))
        self._clients: dict[str, ChomikClient] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ helpers

    def client(self, account: str, fresh: bool = False) -> ChomikClient:
        letter = self.config.letter(account)
        if fresh:
            c = self._client_factory(letter)
            c.login()
            return c
        with self._lock:
            if letter not in self._clients:
                c = self._client_factory(letter)
                c.login()
                self._clients[letter] = c
            return self._clients[letter]

    def _default_reader(self) -> str:
        accounts = self.config.accounts()
        if not accounts:
            raise ValueError("no accounts configured (set CHOMIK_A_USER and CHOMIK_A_PASSWORD)")
        return next(iter(accounts))

    def _parse(self, source: str, as_account: str | None):
        """'A:/Folder' or a URL -> (client, owner, folder path parts, is_own)."""
        if "://" in source or source.startswith("chomikuj.pl"):
            owner, parts = split_path(path_from_url(source if "://" in source else "https://" + source))
            reader = self.client(as_account or self._default_reader())
            return reader, owner, tuple(parts), owner.lower() == reader.username.lower()
        letter, _, path = source.partition(":")
        if not path:
            raise ValueError("use 'A:/Folder/Sub' (account letter, colon, path) or a chomikuj.pl URL")
        c = self.client(letter)
        return c, c.username, tuple(p for p in path.strip("/").split("/") if p), True

    def _own_folder(self, c: ChomikClient, parts: tuple[str, ...]) -> Folder:
        if not parts:
            return Folder("0", "", ())
        f = c.find_folder(list(parts))
        if f is None:
            raise ValueError(f"folder /{'/'.join(parts)} not found in {c.username}")
        return f

    # -------------------------------------------------------------- tools

    def accounts(self) -> list[dict]:
        pw = Passwords.load(self.config.passwords_file)
        out = []
        for letter, user in self.config.accounts().items():
            out.append({
                "account": letter,
                "username": user,
                "delete_protected": ChomikClient(user, password_hash="x").protected,
                "profile_password_configured": pw.profile(user) is not None,
                "folder_passwords_configured": len(pw.folders_of(user)),
            })
        return out

    def list_folders(self, account: str, path: str = "") -> list[dict]:
        c = self.client(account)
        parts = tuple(p for p in path.strip("/").split("/") if p)
        folder = self._own_folder(c, parts)
        return [{"name": f.name, "path": "/" + "/".join(f.path)} for f in c.list_folders(folder.id, folder.path)]

    def list_files(self, source: str, as_account: str | None = None) -> dict:
        c, owner, parts, _ = self._parse(source, as_account)
        listing = c.list_files(chomik_path(owner, parts), missing_ok=True)
        files = [{"name": f.name, "size_bytes": f.size, "id": f.id} for f in listing.files]
        return {"owner": owner, "path": "/" + "/".join(parts), "count": len(files),
                "total_bytes": sum(f["size_bytes"] for f in files), "files": files}

    def download(self, source: str, as_account: str | None = None, recursive: bool = True,
                 max_size_mb: float | None = None) -> dict:
        c, owner, parts, own = self._parse(source, as_account)
        reader = self.client(c.username, fresh=True)
        limit = int(max_size_mb * 1024 * 1024) if max_size_mb else None
        base = _inside(self.config.download_dir, _safe_name(owner), *[_safe_name(p) for p in parts])

        def listings():
            if own:
                folder = self._own_folder(reader, parts)
                folders = [folder, *(reader.walk(folder.id, folder.path) if recursive else [])]
                for f in folders:
                    yield f.path[len(parts):], reader.list_files(chomik_path(owner, f.path), missing_ok=True)
            else:
                url = "https://chomikuj.pl" + chomik_path(owner, parts)
                yield from walk_public(reader, url, max_depth=None if recursive else 0)

        def work(job: Job):
            done = skipped = failed = 0
            got = 0
            for rel, listing in listings():
                for f in listing.files:
                    if limit and f.size > limit:
                        continue
                    dest = _inside(base, *[_safe_name(p) for p in rel], _safe_name(f.name))
                    if os.path.exists(dest) and (not f.size or os.path.getsize(dest) == f.size):
                        skipped += 1
                        continue
                    job.progress.update(current=f.name, done=done, skipped=skipped, failed=failed, bytes=got)
                    try:
                        reader.download_to(f, dest)
                        done += 1
                        got += f.size
                        job.say(f"saved {os.path.relpath(dest, self.config.download_dir)}")
                    except Exception as e:  # noqa: BLE001 - keep going, report per file
                        failed += 1
                        job.say(f"FAILED {f.name}: {e}")
            job.progress.update(current=None, done=done, skipped=skipped, failed=failed, bytes=got)

        job = self.jobs.thread("download", f"{owner}:/{'/'.join(parts)} -> {base}", work)
        return {"job_id": job.id, "saving_to": base}

    def upload(self, local_path: str, account: str, dest_folder: str = "") -> dict:
        src = _inside(self.config.upload_dir, local_path.lstrip("/"))
        if not os.path.exists(src):
            raise ValueError(f"{local_path!r} not found in the upload folder")
        c = self.client(account, fresh=True)
        dest = [p for p in dest_folder.strip("/").split("/") if p]

        def work(job: Job):
            if os.path.isfile(src):
                pairs = [(src, dest)]
            else:
                top = os.path.basename(src.rstrip("/"))
                pairs = []
                for root, _, names in os.walk(src):
                    rel = os.path.relpath(root, src)
                    sub = [] if rel == "." else rel.split(os.sep)
                    pairs += [(os.path.join(root, n), [*dest, top, *sub]) for n in sorted(names)]
            for i, (path, folder) in enumerate(pairs, 1):
                job.progress.update(file=i, of=len(pairs), current=os.path.basename(path))
                c.upload_file(path, folder)
                job.say(f"uploaded {os.path.basename(path)} -> /{'/'.join(folder)}")
            job.progress["current"] = None

        job = self.jobs.thread("upload", f"{local_path} -> {c.username}:/{'/'.join(dest)}", work)
        return {"job_id": job.id}

    def _clone(self, args: list[str], title: str) -> dict:
        state = os.path.join(self.config.state_dir, "clone-state.sqlite")
        argv = [sys.executable, "-m", "chomik_cloner.cli", "--env", self.config.env_file, "--state", state, *args,
                "--passwords", self.config.passwords_file]
        job = self.jobs.process("clone", title, argv)
        return {"job_id": job.id}

    def clone_account(self, src: str, dst: str, only: str | None = None, dest_root: str = "",
                      dry_run: bool = False, folder_copy: bool = True) -> dict:
        s, d = self.config.letter(src), self.config.letter(dst)
        if s == d:
            raise ValueError("source and destination are the same account")
        args = ["account", "--src", s, "--dst", d, "--dest-root", dest_root]
        args += ["--only", only] if only else []
        args += ["--dry-run"] if dry_run else []
        args += [] if folder_copy else ["--no-folder-copy"]
        return self._clone(args, f"{'plan ' if dry_run else ''}{s}:/{only or ''} -> {d}:/{dest_root}")

    def clone_public(self, url: str, dst: str, dest_root: str = "", dry_run: bool = False) -> dict:
        d = self.config.letter(dst)
        args = ["public", url, "--dst", d, "--dest-root", dest_root] + (["--dry-run"] if dry_run else [])
        return self._clone(args, f"{'plan ' if dry_run else ''}{url} -> {d}:/{dest_root}")

    def job_status(self, job_id: str, log_lines: int = 15) -> dict:
        return self.jobs.get(job_id).summary(tail=max(1, min(log_lines, 200)))

    def list_jobs(self) -> list[dict]:
        return [{k: v for k, v in j.summary(tail=1).items() if k != "log_tail"} for j in self.jobs.all()]


def build_server(tools: Tools | None = None) -> MCPServer:
    t = tools or Tools()
    server = MCPServer("chomik", instructions=INSTRUCTIONS, version="0.1.0")

    @server.tool(annotations=READ)
    def accounts() -> list[dict]:
        """Accounts configured on this server, and whether profile/folder passwords are set for them."""
        return t.accounts()

    @server.tool(annotations=READ)
    def list_folders(account: str, path: str = "") -> list[dict]:
        """Subfolders of `path` ("" = root) in one of the configured accounts."""
        return t.list_folders(account, path)

    @server.tool(annotations=READ)
    def list_files(source: str, as_account: str | None = None) -> dict:
        """Files directly in a folder. `source` is "A:/Folder" or a chomikuj.pl URL (read with `as_account`)."""
        return t.list_files(source, as_account)

    @server.tool(annotations=WRITE)
    def download(source: str, as_account: str | None = None, recursive: bool = True,
                 max_size_mb: float | None = None) -> dict:
        """Download a folder ("A:/Folder" or a chomikuj.pl URL) to the server's download folder. Starts a job."""
        return t.download(source, as_account, recursive, max_size_mb)

    @server.tool(annotations=WRITE)
    def upload(local_path: str, account: str, dest_folder: str = "") -> dict:
        """Upload a file or folder from the server's upload folder into `account`/`dest_folder`. Starts a job."""
        return t.upload(local_path, account, dest_folder)

    @server.tool(annotations=WRITE)
    def clone_account(src: str, dst: str, only: str | None = None, dest_root: str = "",
                      dry_run: bool = False, folder_copy: bool = True) -> dict:
        """Copy a whole account (or the folder `only`) into another account, server-side. Resumable. Starts a job."""
        return t.clone_account(src, dst, only, dest_root, dry_run, folder_copy)

    @server.tool(annotations=WRITE)
    def clone_public(url: str, dst: str, dest_root: str = "", dry_run: bool = False) -> dict:
        """Copy someone's public folder (and subfolders) into one of your accounts. Starts a job."""
        return t.clone_public(url, dst, dest_root, dry_run)

    @server.tool(annotations=READ)
    def job_status(job_id: str, log_lines: int = 15) -> dict:
        """Progress and the last log lines of a download/upload/clone job."""
        return t.job_status(job_id, log_lines)

    @server.tool(annotations=READ)
    def list_jobs() -> list[dict]:
        """All jobs started since the server came up."""
        return t.list_jobs()

    return server
