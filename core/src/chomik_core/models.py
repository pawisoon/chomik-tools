"""Plain data objects returned by the client."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Folder:
    id: str
    name: str
    path: tuple[str, ...] = ()  # names from the account root, e.g. ("Books", "PDF")


@dataclass(frozen=True)
class Agreement:
    """How chomikuj wants a download paid for: 'own' (free, your file), 'small', 'transfer', ..."""

    name: str
    cost: int = 0


@dataclass
class RemoteFile:
    id: str
    name: str
    size: int
    agreement: Agreement
    folder_path: str = ""  # chomikuj globalId of the containing folder, e.g. "/user/Books"
    owner: str = ""  # hamsterName
    url: str | None = None  # signed download URL, filled by ChomikClient.resolve_download


@dataclass(frozen=True)
class Balance:
    """accountBalance block that comes with every Download response.

    `transfer_*` are in the units the server reports (MB on observed accounts).
    """

    transfer_value: int
    transfer_limit: int
    transfer_extra: int
    points: int

    @property
    def transfer_available(self) -> int:
        return self.transfer_value + self.transfer_extra


@dataclass
class FolderListing:
    path: str
    owner: str
    files: list[RemoteFile] = field(default_factory=list)
    balance: Balance | None = None
