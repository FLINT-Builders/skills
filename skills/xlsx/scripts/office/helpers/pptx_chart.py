"""Find chart XML that PowerPoint refuses but the schema accepts.

Detection only: for either fault more than one repair is valid, and only the
author knows which was meant.
"""


from __future__ import annotations

import re
from typing import Container, Iterator, Mapping
from xml.etree.ElementTree import Element

from . import parse_part


_CHART_PART_RE = re.compile(r"ppt/charts/chart\d+\.xml")

CHART_NAMESPACES = frozenset({
    "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "http://purl.oclc.org/ooxml/drawingml/chart",
})

_WORD_RE = re.compile(r"\w+")
_INT_RE = re.compile(r"-?\d+")


def _chart_name(element: Element) -> str | None:
    namespace, _, local = element.tag.rpartition("}")
    return local if namespace[1:] in CHART_NAMESPACES else None


def _outside_ext_lst(element: Element) -> Iterator[Element]:
    stack = list(reversed(element))
    while stack:
        child = stack.pop()
        if _chart_name(child) == "extLst":
            continue
        yield child
        stack.extend(reversed(child))


def _outermost(root: Element, names: Container[str]) -> Iterator[Element]:
    # A group nested inside another is checked as part of the outer one, so
    # no subtree is walked more than once.
    stack = [root]
    while stack:
        element = stack.pop()
        if _chart_name(element) in names:
            yield element
            continue
        stack.extend(reversed(element))


def _word_val(element: Element) -> str | None:
    val = element.get("val")
    return val if val is not None and _WORD_RE.fullmatch(val) else None


BAR_GROUPS = frozenset({"bar3DChart", "barChart"})

STACKED_GROUPINGS = frozenset({"stacked", "percentStacked"})
ILLEGAL_ON_STACKED = frozenset({"outEnd"})
LEGAL_ON_STACKED = ("ctr", "inEnd", "inBase")


def _check_stacked_label_positions(part: str, root: Element) -> list[str]:
    problems: list[str] = []
    for chart_group in _outermost(root, BAR_GROUPS):
        group = _chart_name(chart_group)
        grouping = None
        bad: list[str] = []
        for element in _outside_ext_lst(chart_group):
            name = _chart_name(element)
            if name == "grouping" and grouping is None:
                grouping = _word_val(element)
            elif name == "dLblPos":
                pos = _word_val(element)
                if pos in ILLEGAL_ON_STACKED:
                    bad.append(pos)

        if grouping not in STACKED_GROUPINGS:
            continue

        for pos in sorted(set(bad)):
            problems.append(
                f'{part}: {bad.count(pos)} data label(s) use dLblPos="{pos}" on a '
                f"{grouping} {group}; PowerPoint allows only "
                f"{', '.join(LEGAL_ON_STACKED)} there"
            )
    return problems


AXIS_KINDS = frozenset({"catAx", "valAx", "serAx", "dateAx"})

AXID_LIMIT = {
    "barChart": 2, "lineChart": 2, "areaChart": 2, "scatterChart": 2,
    "bubbleChart": 2, "radarChart": 2, "stockChart": 2,
    "bar3DChart": 3, "line3DChart": 3, "area3DChart": 3,
    "surfaceChart": 3, "surface3DChart": 3,
}

AXID_MINIMUM = {
    "barChart": 2, "lineChart": 2, "areaChart": 2, "scatterChart": 2,
    "bubbleChart": 2, "radarChart": 2, "stockChart": 2,
    "bar3DChart": 2, "area3DChart": 2, "surfaceChart": 2,
    "line3DChart": 3, "surface3DChart": 3,
}


def _axis_id(element: Element) -> str | None:
    if _chart_name(element) != "axId" or len(element) or (element.text or "").strip():
        return None
    val = element.get("val")
    return val if val is not None and _INT_RE.fullmatch(val) else None


def _declared_axes(root: Element) -> dict[str, list[str]]:
    axes: dict[str, list[str]] = {}
    for element in root.iter():
        kind = _chart_name(element)
        if kind not in AXIS_KINDS or not len(element):
            continue
        first = element[0]
        if _chart_name(first) != "axId":
            continue
        val = first.get("val")
        if val is not None and _INT_RE.fullmatch(val):
            axes.setdefault(kind, []).append(val)
    return axes


def _canonical_ids(axes: dict[str, list[str]], limit: int) -> list[str] | None:
    category = axes.get("catAx", []) + axes.get("dateAx", [])
    value = axes.get("valAx", [])
    series = axes.get("serAx", [])
    if len(category) != 1 or len(value) != 1 or len(series) > 1:
        return None
    ids = [category[0], value[0]]
    if limit >= 3 and series:
        ids.append(series[0])
    return ids


def _undeclared_axes(kind: str, group: Element, axes: dict[str, list[str]]) -> list[str] | None:
    if kind not in AXID_LIMIT:
        return None
    ids = [i for i in map(_axis_id, group.iter()) if i is not None]
    declared = {i for group in axes.values() for i in group}
    if len([i for i in ids if i in declared]) >= 2:
        return None
    return ids


def _check_chart_axis_references(part: str, root: Element) -> list[str]:
    axes = _declared_axes(root)
    problems: list[str] = []
    declared = {i for group in axes.values() for i in group}
    for group in _outermost(root, AXID_LIMIT):
        kind = _chart_name(group)
        ids = _undeclared_axes(kind, group, axes)
        if ids is None:
            continue
        if not ids:
            problems.append(
                f"{part}: <c:{kind}> declares no <c:axId> this part can resolve; a chart "
                f"group needs {AXID_MINIMUM[kind]}, and PowerPoint discards one with fewer"
            )
            continue
        dead = [i for i in ids if i not in declared]
        canonical = _canonical_ids(axes, AXID_LIMIT[kind])
        if canonical is not None and len(canonical) >= AXID_MINIMUM[kind]:
            hint = f"Fix: point them at the axes this part declares ({', '.join(canonical)})"
        else:
            hint = ("Fix: the part declares several axes of a kind -- declare the "
                    "secondary axes the series expects, or drop them")
        detail = (f"of which {', '.join(dead)} name no declared axis"
                  if dead else f"only {len(ids)} of which this part declares")
        problems.append(
            f"{part}: <c:{kind}> references axId {', '.join(ids)}, {detail}, "
            f"leaving fewer than two live axes; PowerPoint discards the chart. {hint}"
        )
    return problems


CHART_CHECKS = (_check_stacked_label_positions, _check_chart_axis_references)


def find_chart_problems(files: Mapping[str, bytes]) -> list[str]:
    problems: list[str] = []
    for part in sorted(n for n in files if _CHART_PART_RE.fullmatch(n)):
        root = parse_part(files[part])
        if root is None:
            problems.append(f"{part}: could not be parsed, so its chart checks did not run")
            continue
        for check in CHART_CHECKS:
            problems.extend(check(part, root))
    return problems
