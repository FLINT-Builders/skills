"""Find masters sharing a theme part in the way PowerPoint refuses to open.

Reports only; the fix is to move <p:notesMasterIdLst> back to directly after
<p:sldIdLst> in ppt/presentation.xml.
"""


from __future__ import annotations

import posixpath
import re
from typing import Mapping
from xml.parsers.expat import ExpatError

import defusedxml.minidom

THEME_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"

_MASTER_RE = re.compile(
    r"^ppt/(?P<group>slideMasters|notesMasters|handoutMasters)/"
    r"(?:slide|notes|handout)Master(?P<num>\d+)\.xml$"
)
_GROUP_ORDER = {"slideMasters": 0, "notesMasters": 1, "handoutMasters": 2}


def _parse(data: bytes):
    try:
        return defusedxml.minidom.parseString(data)
    except (ExpatError, ValueError):
        return None


def _sort_key(name: str) -> tuple[int, int]:
    m = _MASTER_RE.match(name)
    assert m is not None
    return (_GROUP_ORDER[m.group("group")], int(m.group("num")))


def _rels_path(part: str) -> str:
    directory, base = posixpath.split(part)
    return f"{directory}/_rels/{base}.rels"


def _resolve(rels_path: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    part_dir = posixpath.dirname(posixpath.dirname(rels_path))
    return posixpath.normpath(posixpath.join(part_dir, target))


def _theme_rel(files: Mapping[str, bytes], master: str):
    rels_path = _rels_path(master)
    rels = files.get(rels_path)
    if rels is None:
        return None
    dom = _parse(rels)
    if dom is None:
        return None
    for element in dom.getElementsByTagName("Relationship"):
        if element.getAttribute("Type") != THEME_REL_TYPE:
            continue
        target = element.getAttribute("Target")
        if not target:
            continue
        return rels_path, element, _resolve(rels_path, target)
    return None


def _masters(files: Mapping[str, bytes]) -> list[str]:
    return sorted((n for n in files if _MASTER_RE.match(n)), key=_sort_key)


_PRESENTATION = "ppt/presentation.xml"
_NOTES_MASTERS = "ppt/notesMasters/"


def _notes_master_share_is_inert(files: Mapping[str, bytes]) -> bool:
    data = files.get(_PRESENTATION)
    if data is None:
        return False
    dom = _parse(data)
    if dom is None:
        return False
    lists = dom.getElementsByTagName("p:sldIdLst")
    if not lists:
        return False
    node = lists[0].nextSibling
    while node is not None:
        if node.nodeType == node.ELEMENT_NODE:
            return node.tagName == "p:notesMasterIdLst"
        blank = node.nodeType == node.TEXT_NODE and not node.data.strip(" \t\r\n")
        if not blank and node.nodeType not in (node.COMMENT_NODE, node.PROCESSING_INSTRUCTION_NODE):
            return False
        node = node.nextSibling
    return False


def _shares(files: Mapping[str, bytes]):
    owner: dict[str, str] = {}
    for master in _masters(files):
        found = _theme_rel(files, master)
        if found is None:
            continue
        rels_path, element, theme = found
        if theme not in files:
            continue  
        if theme in owner:
            yield master, rels_path, element, theme, owner[theme]
        else:
            owner[theme] = master


def _is_inert(master: str, inert_notes: bool) -> bool:
    return inert_notes and master.startswith(_NOTES_MASTERS)


def find_shared_master_themes(files: Mapping[str, bytes]) -> list[str]:
    return [
        f"{master} shares {theme} with {first}"
        for master, _, _, theme, first in _shares(files)
    ]


def live_shared_master_themes(files: Mapping[str, bytes]) -> list[str]:
    inert_notes = _notes_master_share_is_inert(files)
    return [
        f"{master} shares {theme} with {first}"
        for master, _, _, theme, first in _shares(files)
        if not _is_inert(master, inert_notes)
    ]
