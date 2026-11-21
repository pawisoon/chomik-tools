"""Sonarr/Radarr bridge: chomikuj.pl search as a Newznab indexer, downloads through a SABnzbd-compatible API."""
from .bridge import ArrBridge, ArrConfig

__all__ = ["ArrBridge", "ArrConfig"]
