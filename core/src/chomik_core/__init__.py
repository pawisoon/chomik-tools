"""Shared ChomikBox client for chomikuj.pl tools."""
from .client import ChomikClient, DownloadStream
from .errors import ApiError, AuthError, ChomikError, DownloadError, ProtectedAccountError, ProtocolError, UploadError
from .models import Agreement, Balance, Folder, FolderListing, RemoteFile
from .names import chomik_path, decode_segment, encode_segment, path_from_url, split_path
from .public import list_public_subfolders, walk_public
from .relay import relay_file
from .search import SearchHit, SearchPage, Searcher, search_files

__all__ = [
    "ChomikClient", "DownloadStream",
    "ChomikError", "ApiError", "AuthError", "DownloadError", "ProtectedAccountError", "ProtocolError", "UploadError",
    "Agreement", "Balance", "Folder", "FolderListing", "RemoteFile",
    "chomik_path", "decode_segment", "encode_segment", "path_from_url", "split_path",
    "list_public_subfolders", "walk_public", "relay_file",
    "SearchHit", "SearchPage", "Searcher", "search_files",
]
__version__ = "0.1.0"
