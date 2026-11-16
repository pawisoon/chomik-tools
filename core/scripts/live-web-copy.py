"""Live check of server-side copy A -> B (copies ONE file, deletes nothing).

Needs in .env: CHOMIK_A_USER, CHOMIK_B_USER, CHOMIK_B_PASSWORD (plain, web login),
CHOMIK_A_PROFILE_PASSWORD if A is password-protected. Folder passwords: chomik-passwords.json.

Usage: .venv/bin/python scripts/live-web-copy.py [FILE_ID] [SOURCE_FOLDER_PATH]
Default: the Audi PDF in "Audi Instrukcja Obsługi" on A.
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

from chomik_core import ChomikClient, chomik_path  # noqa: E402
from chomik_core.envfile import load_env_file  # noqa: E402
from chomik_core.passwords import Passwords  # noqa: E402
from chomik_core.web import ChomikWeb  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
load_env_file(".env")

file_id = sys.argv[1] if len(sys.argv) > 1 else "8706373657"
src_account = os.environ["CHOMIK_A_USER"]

b = ChomikClient.from_env("CHOMIK_B_")
b.login()
dest = b.mkdir_p("chomik-clone-test/server-side")
before = {f.id for f in b.list_files(chomik_path(b.username, dest.path), missing_ok=True).files}

w = ChomikWeb.from_env("CHOMIK_B_")
passwords = Passwords.load()
print("passwords configured:", passwords)  # names only, never values
w.apply_passwords(passwords, src_account)

t0 = time.monotonic()
msg = w.copy_file(file_id, dest.id)
print(f"site says ({time.monotonic() - t0:.1f}s): {msg[:200]}")

for i in range(5):
    time.sleep(3)
    files = b.list_files(chomik_path(b.username, dest.path), missing_ok=True).files
    new = [f for f in files if f.id not in before]
    if new:
        for f in new:
            print(f"ARRIVED on B: {f.name} ({f.size} bytes, id {f.id})")
        sys.exit(0)
print("nothing new on B after 15 s")
sys.exit(1)
