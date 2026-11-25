"""Parse an XML part once, keeping each element's byte offsets.

Edits are spliced into the original bytes, so everything outside the edit stays
as it was: round-tripping through ElementTree rewrites namespace prefixes, and
xml.dom.minidom builds each text node by repeated concatenation, which is
quadratic in the length of a long text or CDATA run.
"""


from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree.ElementTree import ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import DefusedXMLParser

_TAG_DELIMITER_RE = re.compile(rb"[\"'>]")
_QNAME_RE = re.compile(rb"<([^\s/>]+)")


class XmlPartError(ValueError):
    """The part is not well-formed UTF-8 XML, or uses a forbidden construct."""


@dataclass(eq=False)
class XmlElement:
    tag: str
    attrib: dict[str, str]
    parent: XmlElement | None
    start: int
    end_event: int = -1

    @property
    def local_name(self) -> str:
        return self.tag.rpartition("}")[2]


class XmlPart:
    def __init__(self, raw: bytes, where: str | Path):
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise XmlPartError(f"{where} is not UTF-8") from e
        self.raw = raw
        self.elements: list[XmlElement] = []
        self._open: list[XmlElement] = []
        self._parser = DefusedXMLParser(target=self)
        try:
            self._parser.feed(raw)
            self._parser.close()
        except (ParseError, DefusedXmlException) as e:
            raise XmlPartError(f"cannot parse {where}: {e}") from e
        finally:
            del self._parser

    @classmethod
    def read(cls, path: Path) -> XmlPart:
        return cls(path.read_bytes(), path)

    def start(self, tag: str, attrib: dict[str, str]) -> None:
        parent = self._open[-1] if self._open else None
        element = XmlElement(tag, attrib, parent, self._parser.parser.CurrentByteIndex)
        self.elements.append(element)
        self._open.append(element)

    def end(self, tag: str) -> None:
        self._open.pop().end_event = self._parser.parser.CurrentByteIndex

    def data(self, text: str) -> None:
        pass

    def close(self) -> None:
        return None

    @property
    def root(self) -> XmlElement:
        return self.elements[0]

    def children(self, parent: XmlElement, tag: str) -> list[XmlElement]:
        return [e for e in self.elements if e.parent is parent and e.tag == tag]

    def tag_end(self, offset: int) -> int:
        pos = offset
        while (m := _TAG_DELIMITER_RE.search(self.raw, pos)) is not None:
            if m.group(0) == b">":
                return m.end()
            pos = self.raw.index(m.group(0), m.end()) + 1
        raise XmlPartError("unterminated tag in a part the parser accepted")

    def is_empty_tag(self, element: XmlElement) -> bool:
        end = self.tag_end(element.start)
        return self.raw[end - 2 : end] == b"/>"

    def element_end(self, element: XmlElement) -> int:
        if self.is_empty_tag(element):
            return self.tag_end(element.start)
        return self.tag_end(element.end_event)

    def qname(self, element: XmlElement) -> bytes:
        return _QNAME_RE.match(self.raw, element.start).group(1)

    def without(self, elements: list[XmlElement]) -> bytes:
        pieces = []
        copied_to = 0
        for element in sorted(elements, key=lambda e: e.start):
            if element.start < copied_to:
                continue
            pieces.append(self.raw[copied_to : element.start])
            copied_to = self.element_end(element)
        pieces.append(self.raw[copied_to:])
        return b"".join(pieces)
