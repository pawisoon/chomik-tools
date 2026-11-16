"""Read-only checks against the real API, once per configured account.

Credentials come from .env (see .env.example): CHOMIK_A_* and CHOMIK_B_*.
Run: ./scripts/live-test.sh   (or: pytest -m live)
"""
import logging
import os
import pathlib

import pytest

from chomik_core import ChomikClient
from chomik_core.envfile import load_env_file

load_env_file(str(pathlib.Path(__file__).resolve().parent.parent / ".env"))

ACCOUNTS = ["A", "B"]
pytestmark = pytest.mark.live
log = logging.getLogger("chomik_core")


@pytest.fixture(scope="module", params=ACCOUNTS)
def client(request):
    prefix = f"CHOMIK_{request.param}_"
    if not os.environ.get(prefix + "USER"):
        pytest.skip(f"account {request.param}: {prefix}USER not set")
    c = ChomikClient.from_env(prefix)
    c.login()
    return c


def test_root_folders(client):
    folders = client.list_folders()
    assert isinstance(folders, list)
    log.info("%s: %d root folders", client.username, len(folders))


def test_list_files_via_encoded_path(client):
    """Verifies chomik_path() encoding is accepted by SOAP Download (incl. non-ASCII names)."""
    checked = []
    for folder in client.walk():
        if not any(ord(ch) > 127 or ch == " " for ch in "".join(folder.path)):
            continue
        listing = client.list_files_in(folder)
        assert listing.balance is not None
        assert listing.owner, f"empty response for {folder.path}; path encoding likely wrong"
        log.info("OK %s -> %s (%d files)", "/".join(folder.path), listing.path, len(listing.files))
        checked.append(folder.path)
        if len(checked) >= 3:
            break
    if not checked:
        pytest.skip(f"{client.username}: no folder with spaces or non-ASCII names to verify encoding")


def test_balance(client):
    # Root path with no files is rejected ("requested file(s) not available"); use a folder.
    folders = client.list_folders()
    if not folders:
        pytest.skip(f"{client.username}: no folders to read balance from")
    client.list_files_in(folders[0], missing_ok=True)
    assert client.balance is not None
    b = client.balance
    log.info("%s: transfer %d/%d + extra %d, points %d",
             client.username, b.transfer_value, b.transfer_limit, b.transfer_extra, b.points)
