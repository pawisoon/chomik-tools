"""Copy a file between accounts by piping the download straight into the upload.

Nothing touches disk. The byte count comes from the download's Content-Length
(falls back to the listed size), because the upload must declare it up front.
"""
from __future__ import annotations

from .client import ChomikClient, ProgressFn
from .errors import DownloadError
from .models import Folder, RemoteFile


def relay_file(
    src: ChomikClient,
    f: RemoteFile,
    dst: ChomikClient,
    dest_folder: Folder | str,
    *,
    name: str | None = None,
    on_progress: ProgressFn | None = None,
) -> str:
    """Stream `f` (readable by `src`) into `dst` under `dest_folder`. Returns stored name."""
    folder_id = dest_folder.id if isinstance(dest_folder, Folder) else str(dest_folder)
    with src.open_download(f) as stream:
        if stream.size <= 0:
            raise DownloadError(f"{f.name}: unknown size, cannot relay without spooling")
        return dst.upload(stream, stream.size, name or f.name, folder_id, on_progress)
