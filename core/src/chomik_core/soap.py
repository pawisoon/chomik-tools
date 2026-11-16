"""Tiny SOAP helpers for the ChomikBox service.

The server mixes the namespaces ``http://chomikuj.pl/`` and ``http://chomikuj.pl``
(with and without trailing slash), so all lookups match on local tag names.
"""
from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from typing import Iterator

from .errors import ProtocolError

SOAP_URL = "https://box.chomikuj.pl/services/ChomikBoxService.svc"
SOAP_ACTION_PREFIX = "http://chomikuj.pl/IChomikBoxService/"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"


def esc(value: object) -> str:
    """Escape a value for use as XML text content."""
    return html.escape(str(value), quote=False)


def envelope(action: str, inner_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<{action} xmlns="http://chomikuj.pl/">{inner_xml}</{action}>'
        "</s:Body></s:Envelope>"
    )


def headers(action: str) -> dict[str, str]:
    return {
        "SOAPAction": SOAP_ACTION_PREFIX + action,
        "Content-Type": "text/xml;charset=utf-8",
    }


def parse(xml_text: str) -> ET.Element:
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ProtocolError(f"invalid XML from server: {e}; body starts {xml_text[:200]!r}") from e


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child(parent: ET.Element | None, name: str) -> ET.Element | None:
    """First direct child with the given local name."""
    if parent is None:
        return None
    for c in parent:
        if localname(c.tag) == name:
            return c
    return None


def children(parent: ET.Element | None, name: str) -> Iterator[ET.Element]:
    if parent is None:
        return
    for c in parent:
        if localname(c.tag) == name:
            yield c


def find(parent: ET.Element | None, name: str) -> ET.Element | None:
    """First descendant (depth-first, including parent) with the given local name."""
    if parent is None:
        return None
    for el in parent.iter():
        if localname(el.tag) == name:
            return el
    return None


def is_nil(el: ET.Element | None) -> bool:
    return el is None or el.get(XSI_NIL) == "true"


def text(parent: ET.Element | None, name: str, default: str | None = None) -> str | None:
    el = child(parent, name)
    if is_nil(el):
        return default
    return (el.text or "").strip()
