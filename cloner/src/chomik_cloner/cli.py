"""chomik-clone CLI.

  chomik-clone account --src A --dst B [--only "Folder/Sub"] [--dest-root "Klon"] [--dry-run]
  chomik-clone public URL --dst B [--dest-root "Z publicznych"] [--dry-run]

Accounts are letters mapped to CHOMIK_<X>_USER / _PASSWORD / _PASSWORD_HASH in .env.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter

from chomik_core import ChomikClient, ChomikError
from chomik_core.envfile import load_env_file
from chomik_core.names import path_from_url, split_path
from chomik_core.passwords import Passwords
from chomik_core.web import ChomikWeb, FolderLocked

from .clone import (DEFAULT_SKIP_DIRS, Plan, account_items, build_plan, public_items, run, server_copier,
                    server_folder_copier, stream_copier, web_lister)
from .state import State


def _client(letter: str) -> ChomikClient:
    c = ChomikClient.from_env(f"CHOMIK_{letter.upper()}_")
    c.login()
    return c


def _gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB"


def print_plan(plan: Plan, src_label: str, dst: ChomikClient, dest_root: tuple[str, ...]) -> None:
    print(f"\nPlan: {src_label} -> {dst.username}:/{'/'.join(dest_root)}")
    print(f"  folders scanned : {plan.folders}")
    print(f"  files           : {len(plan.items)}")
    print(f"  total size      : {_gb(plan.total_bytes)}")
    by_top = Counter()
    count_top = Counter()
    for i in plan.items:
        key = i.rel[0] if i.rel else "(root)"
        by_top[key] += i.file.size
        count_top[key] += 1
    if by_top:
        print("  largest top-level folders:")
        for name, size in by_top.most_common(10):
            print(f"    {_gb(size):>10}  {count_top[name]:>6} files  {name}")
    agreements = Counter(i.file.agreement.name for i in plan.items)
    print(f"  agreements      : {dict(agreements)}")
    if plan.list_errors:
        print(f"  listing errors  : {len(plan.list_errors)} (first: {plan.list_errors[0]})")
    if dst.balance:
        print(f"  dst transfer    : {dst.balance.transfer_value}/{dst.balance.transfer_limit} + extra {dst.balance.transfer_extra}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chomik-clone", description="Copy files into another chomikuj account. Never deletes.")
    p.add_argument("--env", default=".env", help="env file with CHOMIK_<X>_* credentials (default .env)")
    p.add_argument("--state", default="clone-state.sqlite", help="resume database")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("account", "public"):
        sp = sub.add_parser(name)
        if name == "account":
            sp.add_argument("--src", required=True, help="source account letter, e.g. A")
            sp.add_argument("--only", help="copy only this folder path of the source (and below)")
        else:
            sp.add_argument("url", help="public chomikuj folder URL")
        sp.add_argument("--dst", required=True, help="destination account letter, e.g. B")
        sp.add_argument("--dest-root", default="", help="folder in destination to copy under (default: root)")
        sp.add_argument("--dry-run", action="store_true", help="scan and print the plan, copy nothing")
        sp.add_argument("--max-files", type=int, help="stop planning after N files (for tests)")
        sp.add_argument("--max-size", type=int, help="skip files larger than N bytes")
        sp.add_argument("--method", choices=["server", "stream"], default="server",
                        help="server: site-side copy, no transfer through you (default); stream: download+upload")
        sp.add_argument("--fallback-stream", action="store_true",
                        help="stream files the site refuses or that never arrive (slow: goes through this machine)")
        sp.add_argument("--no-verify", action="store_true", help="trust the copy answer, skip arrival checks")
        sp.add_argument("--pause", type=float, default=5.0,
                        help="seconds between copies (default 5; faster runs saw many silently dropped copies)")
        sp.add_argument("--skip-dir", action="append", metavar="NAME",
                        help=f"skip folders with this name and all below; repeatable (default: {', '.join(DEFAULT_SKIP_DIRS)})")
        sp.add_argument("--no-skip", action="store_true", help="copy every folder, including the default skips")
        sp.add_argument("--no-folder-copy", action="store_true",
                        help="never use the site's whole-folder copy (default: used for folders the destination lacks)")
        sp.add_argument("--passwords", default="chomik-passwords.json", help="profile/folder passwords JSON")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env_file(a.env)

    skip_dirs = () if a.no_skip else tuple(a.skip_dir or DEFAULT_SKIP_DIRS)
    try:
        dst = _client(a.dst)
        dest_root = tuple(x for x in a.dest_root.strip("/").split("/") if x)
        src = _client(a.src) if a.cmd == "account" else dst  # public files are read with the destination's token
        if a.cmd == "account" and src.username.lower() == dst.username.lower():
            print("source and destination are the same account", file=sys.stderr)
            return 2
        source_account = src.username if a.cmd == "account" else split_path(path_from_url(a.url))[0]

        copier, lister, fallback, source_visible, folder_copier = stream_copier, None, None, None, None
        web = None
        passwords = Passwords.load(a.passwords)
        try:
            web = ChomikWeb.from_env(f"CHOMIK_{a.dst.upper()}_")
            web.login()
            web.apply_passwords(passwords, source_account)
            lister = web_lister(web, dst.username)  # reliable listing for skip + verify
        except (ValueError, ChomikError) as e:
            if a.method == "server":
                raise
            web = None
            print(f"note: no web session ({e}); verifying with the SOAP listing", file=sys.stderr)

        if a.cmd == "account":
            web_files = (lambda fid: web.list_files(source_account, fid)) if web else None
            source = account_items(src, a.only, skip_dirs, web_files=web_files)
            label = f"{src.username}:/{a.only or ''}"
        else:
            source = public_items(dst, a.url)
            label = a.url

        plan = build_plan(source, max_files=a.max_files, max_size=a.max_size, skip_dirs=skip_dirs)
        print_plan(plan, label, dst, dest_root)
        if a.dry_run:
            return 0

        state = State(a.state)
        if a.method == "server":

            def relogin():
                web.logged_in = False
                web.login()
                web.apply_passwords(passwords, source_account)

            copier = server_copier(web, relogin=relogin)
            visible_cache: dict[str, bool] = {}

            def source_visible(folder_id: str) -> bool:
                if folder_id not in visible_cache:
                    try:
                        web.list_files(source_account, folder_id, max_pages=1)
                        visible_cache[folder_id] = True
                    except FolderLocked:
                        visible_cache[folder_id] = False
                return visible_cache[folder_id]
            fallback = stream_copier if a.fallback_stream else None
            # A folder copy takes every file in the folder, so not when the plan filters files.
            if not (a.no_folder_copy or a.max_size or a.max_files):
                folder_copier = server_folder_copier(web, source_account)

        def on_file(n, total, item, msg):
            print(f"[{n}/{total}] {'/'.join(item.rel)}/{item.file.name}: {msg}", flush=True)

        res = run(plan, src, dst, dest_root, state, copier=copier, lister=lister, fallback=fallback,
                  verify_delays=() if a.no_verify else (5, 20, 40), pause=a.pause,
                  source_visible=source_visible, folder_copier=folder_copier, on_file=on_file)
        print(f"\nDone: verified {res.copied} ({_gb(res.copied_bytes)}), skipped {res.skipped}, "
              f"failed {len(res.failed)}, missing {len(res.missing)}")
        if res.locked:
            print(f"  locked source folders: {len(res.locked)} ({sum(n for _, n in res.locked)} files); add passwords and rerun:")
            for label, n in res.locked[:30]:
                print(f"    LOCKED {n:>5} files  {label}")
        for path, size in res.missing[:50]:
            print(f"  MISSING {_gb(size):>10}  {path}")
        for path, err in res.failed[:20]:
            print(f"  FAILED {path}: {err}")
        if res.stopped:
            print(f"STOPPED EARLY: {res.stopped}")
        return 1 if res.failed or res.missing or res.stopped else 0
    except ChomikError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
