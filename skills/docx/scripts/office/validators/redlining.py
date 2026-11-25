"""
Validator for tracked changes in Word documents.

Detects untracked edits: text that differs from the original without a
<w:ins>/<w:del> wrapper recording it. Every WordprocessingML story part is
compared on its own: the main document, headers, footers, footnotes, endnotes
and the glossary document, found by their root element wherever they sit in
the package. In each part the tracked changes that are new relative to the
original are undone, and the result is compared against the part at the same
path in the original; whatever text still differs was edited without being
tracked. A part present in only one of the two files is compared against an
empty part.

References that pull text in from elsewhere are compared by the text they
show: header and footer references, footnote and endnote references, and
w:altChunk imports. Pointing one at different content is reported even when
no part's own text changed.

Comments are annotations and are not compared. Neither is formatting, nor
content that cannot carry a tracked change: charts, SmartArt, images and
embedded objects.
"""

import difflib
import hashlib
import posixpath
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException

from helpers import opc_target, rendered_text, safe_extract

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
# ISO/IEC 29500 Strict spellings of the same vocabularies; Word opens either.
STRICT_NAMESPACES = {
    "http://purl.oclc.org/ooxml/wordprocessingml/main": W_NS,
    "http://purl.oclc.org/ooxml/officeDocument/relationships": R_NS,
}
UNCOMPARED_ROOTS = {"comments"}


def _split_tag(name):
    if name.startswith("{"):
        ns, _, local = name[1:].partition("}")
        return ns, local
    return "", name


def _normalize_namespaces(root):
    def fix(name):
        ns, local = _split_tag(name)
        if ns in STRICT_NAMESPACES:
            return f"{{{STRICT_NAMESPACES[ns]}}}{local}"
        return name

    for elem in root.iter():
        if isinstance(elem.tag, str):
            elem.tag = fix(elem.tag)
        for key in list(elem.attrib):
            fixed = fix(key)
            if fixed != key:
                elem.attrib[fixed] = elem.attrib.pop(key)
    return root


def load_story_parts(base):
    """Parse every WordprocessingML story part under `base`, keyed by part name.

    Parts are recognised by their root element, not their file name, because
    Word reaches them through relationships and content types. Returns the
    parts and a list of files that could not be parsed: any `.xml` file, or a
    file of any name that defusedxml refused.
    """
    base = Path(base)
    parts, errors = {}, []
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        name = path.relative_to(base).as_posix()
        try:
            root = ET.parse(path).getroot()
        except Exception as e:
            if isinstance(e, DefusedXmlException) or path.suffix.lower() == ".xml":
                errors.append(f"{name}: {e}")
            continue
        _normalize_namespaces(root)
        ns, local = _split_tag(root.tag)
        if ns == W_NS and local not in UNCOMPARED_ROOTS:
            parts[name] = root
    return parts, errors


def _relationship_targets(base, part_name):
    rels = (
        Path(base)
        / posixpath.dirname(part_name)
        / "_rels"
        / f"{posixpath.basename(part_name)}.rels"
    )
    try:
        root = ET.parse(rels).getroot()
    except Exception:
        return {}
    targets = {}
    for rel in root.iter(f"{{{REL_NS}}}Relationship"):
        try:
            targets[rel.get("Id")] = opc_target(
                rel.get("Target", ""), part_name, rel.get("TargetMode", "")
            )
        except ValueError:
            targets[rel.get("Id")] = None
    return targets


class RedliningValidator:

    def __init__(self, unpacked_dir, original_docx, verbose=False):
        self.unpacked_dir = Path(unpacked_dir)
        self.original_docx = Path(original_docx)
        self.verbose = verbose
        self.namespaces = {
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        }

    def repair(self) -> int:
        return 0

    def validate(self):
        modified_file = self.unpacked_dir / "word" / "document.xml"
        if not modified_file.exists():
            print(f"FAILED - Modified document.xml not found at {modified_file}")
            return False

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            try:
                with zipfile.ZipFile(self.original_docx, "r") as zip_ref:
                    safe_extract(zip_ref, temp_path)
            except Exception as e:
                print(f"FAILED - Error unpacking original docx: {e}")
                return False

            original_file = temp_path / "word" / "document.xml"
            if not original_file.exists():
                print(
                    f"FAILED - Original document.xml not found in {self.original_docx}"
                )
                return False

            modified_parts, modified_errors = load_story_parts(self.unpacked_dir)
            original_parts, original_errors = load_story_parts(temp_path)
            errors = [f"modified {e}" for e in modified_errors] + [
                f"original {e}" for e in original_errors
            ]
            if errors:
                print("FAILED - Error parsing XML files:")
                for error in errors:
                    print(f"  {error}")
                return False

            names = sorted(set(original_parts) | set(modified_parts))
            tracked = 0
            for name in names:
                original_root = original_parts.get(name, Element("empty"))
                modified_root = modified_parts.get(name, Element("empty"))
                new_changes = self._new_tracked_changes(original_root, modified_root)
                self._remove_tracked_changes(modified_root, new_changes)
                tracked += len(new_changes)

            original_views = self._package_views(temp_path, original_parts)
            modified_views = self._package_views(self.unpacked_dir, modified_parts)

            mismatches = [
                (name, original_views.get(name, ""), modified_views.get(name, ""))
                for name in names
                if original_views.get(name, "") != modified_views.get(name, "")
            ]
            if mismatches:
                print(self._generate_detailed_diff(mismatches))
                return False

            if self.verbose:
                print(
                    f"PASSED - All {tracked} change(s) against the original "
                    f"are properly tracked across {len(names)} part(s)"
                )
            return True

    def _package_views(self, base, parts):
        w = self.namespaces["w"]
        notes = {"footnote": {}, "endnote": {}}
        for root in parts.values():
            for kind, bucket in notes.items():
                for note in root.iter(f"{{{w}}}{kind}"):
                    text = self._extract_text_content(note)
                    if text:
                        bucket.setdefault(note.get(f"{{{w}}}id"), []).append(text)

        texts = {name: self._extract_text_content(root) for name, root in parts.items()}
        return {
            name: self._part_view(base, name, root, texts, notes)
            for name, root in parts.items()
        }

    def _part_view(self, base, name, root, texts, notes):
        w = self.namespaces["w"]
        p_tag = f"{{{w}}}p"
        rid = f"{{{R_NS}}}id"
        targets = None

        def target_of(elem):
            nonlocal targets
            if targets is None:
                targets = _relationship_targets(base, name)
            return targets.get(elem.get(rid))

        lines = []
        for elem in root.iter():
            if not isinstance(elem.tag, str):
                continue
            ns, local = _split_tag(elem.tag)
            if elem.tag == p_tag:
                text = self._paragraph_text(elem)
                if text:
                    lines.append(text)
            elif ns != w:
                continue
            elif local in ("headerReference", "footerReference"):
                shown = texts.get(target_of(elem), "")
                if shown:
                    kind = local[: -len("Reference")]
                    lines.append(
                        f"[{kind} {elem.get(f'{{{w}}}type', 'default')}: {shown}]"
                    )
            elif local in ("footnoteReference", "endnoteReference"):
                kind = local[: -len("Reference")]
                shown = " | ".join(notes[kind].get(elem.get(f"{{{w}}}id"), []))
                if shown:
                    lines.append(f"[{kind}: {shown}]")
            elif local == "altChunk":
                target = target_of(elem)
                path = Path(base) / target if target else None
                if path is not None and path.is_file():
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    lines.append(f"[altChunk {target}: sha256 {digest}]")
                else:
                    lines.append("[altChunk: unresolved]")
        return "\n".join(lines)

    def _tracked_change_elements(self, root):
        ins_tag = f"{{{self.namespaces['w']}}}ins"
        del_tag = f"{{{self.namespaces['w']}}}del"
        return [elem for elem in root.iter() if elem.tag in (ins_tag, del_tag)]

    def _rendered_text(self, elem):
        preserve = elem.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"
        return rendered_text(elem.text or "", preserve)

    def _text_elements(self, elem):
        w = self.namespaces["w"]
        return [
            node
            for node in elem.iter()
            if node.tag in (f"{{{w}}}t", f"{{{w}}}delText")
        ]

    def _tracked_change_key(self, elem):
        w = self.namespaces["w"]
        text = "".join(self._rendered_text(node) for node in self._text_elements(elem))
        return (elem.tag, elem.get(f"{{{w}}}author"), elem.get(f"{{{w}}}date"), text)

    def _new_tracked_changes(self, original_root, modified_root):
        original = self._tracked_change_elements(original_root)
        modified = self._tracked_change_elements(modified_root)

        pool = {}
        for elem in original:
            pool.setdefault(self._tracked_change_key(elem), []).append(elem)

        matched, leftover = set(), []
        for elem in modified:
            bucket = pool.get(self._tracked_change_key(elem))
            if bucket:
                matched.add(bucket.pop())
            else:
                leftover.append(elem)

        def group(elem):
            return self._tracked_change_key(elem)[:3]

        def text_of(elems):
            return "".join(self._tracked_change_key(e)[3] for e in elems)

        unmatched_original = {}
        for elem in original:
            if elem not in matched:
                unmatched_original.setdefault(group(elem), []).append(elem)

        by_group = {}
        for elem in leftover:
            by_group.setdefault(group(elem), []).append(elem)

        new = set()
        for key, elems in by_group.items():
            rebuilt = text_of(elems)
            if rebuilt and rebuilt == text_of(unmatched_original.get(key, [])):
                continue  
            new.update(elems)
        return new

    def _generate_detailed_diff(self, mismatches):
        error_parts = [
            "FAILED - Document text doesn't match after removing the tracked changes",
            "in: " + ", ".join(name for name, _, _ in mismatches),
            "",
            "Likely causes:",
            "  1. Modified text inside another author's <w:ins> or <w:del> tags",
            "  2. Made edits without proper tracked changes",
            "  3. Didn't nest <w:del> inside <w:ins> when deleting another's insertion",
            "  4. Rewrote another author's <w:ins>/<w:del> and changed its text on",
            "     the way. A tracked change from the original is recognised by its",
            "     author, date and text; anything that doesn't reproduce one exactly",
            "     reads as new, and the text it carried is reported missing.",
            "  5. Pointed a header, footer, footnote or endnote reference, or a",
            "     <w:altChunk>, at different content. Those show as [bracketed]",
            "     lines carrying the text they display.",
            "",
            "Headers, footers, footnotes and endnotes are checked like the body:",
            "edit them with <w:ins>/<w:del> too.",
            "",
            "For pre-redlined documents, use correct patterns:",
            "  - To reject another's INSERTION: Nest <w:del> inside their <w:ins>",
            "  - To reject PART of one: nest <w:del> around only the runs you reject.",
            "    Their <w:ins> may be split around it, so long as the pieces keep",
            "    their author and date and still spell out the same text.",
            "  - To restore another's DELETION: Add new <w:ins> AFTER their <w:del>",
            "",
        ]

        for name, original_text, modified_text in mismatches:
            heading = f"Differences in {name}:"
            error_parts.extend([heading, "=" * len(heading)])
            git_diff = self._get_git_word_diff(original_text, modified_text)
            if git_diff:
                error_parts.append(git_diff)
            else:
                error_parts.append("Unable to generate word diff (git not available)")
            error_parts.append("Changed lines (- original, + modified):")
            error_parts.extend(
                line
                for line in difflib.ndiff(
                    original_text.splitlines(), modified_text.splitlines()
                )
                if line.startswith(("- ", "+ "))
            )
            error_parts.append("")

        return "\n".join(error_parts).rstrip()

    def _get_git_word_diff(self, original_text, modified_text):
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)

                original_file = temp_path / "original.txt"
                modified_file = temp_path / "modified.txt"

                original_file.write_text(original_text, encoding="utf-8")
                modified_file.write_text(modified_text, encoding="utf-8")

                result = subprocess.run(
                    [
                        "git",
                        "diff",
                        "--word-diff=plain",
                        "--word-diff-regex=.",  
                        "-U0",  
                        "--no-index",
                        str(original_file),
                        str(modified_file),
                    ],
                    capture_output=True,
                    text=True,
                )

                if result.stdout.strip():
                    lines = result.stdout.split("\n")
                    content_lines = []
                    in_content = False
                    for line in lines:
                        if line.startswith("@@"):
                            in_content = True
                            continue
                        if in_content and line.strip():
                            content_lines.append(line)

                    if content_lines:
                        return "\n".join(content_lines)

                result = subprocess.run(
                    [
                        "git",
                        "diff",
                        "--word-diff=plain",
                        "-U0",  
                        "--no-index",
                        str(original_file),
                        str(modified_file),
                    ],
                    capture_output=True,
                    text=True,
                )

                if result.stdout.strip():
                    lines = result.stdout.split("\n")
                    content_lines = []
                    in_content = False
                    for line in lines:
                        if line.startswith("@@"):
                            in_content = True
                            continue
                        if in_content and line.strip():
                            content_lines.append(line)
                    return "\n".join(content_lines)

        except (subprocess.CalledProcessError, FileNotFoundError, Exception):
            pass

        return None

    def _remove_tracked_changes(self, root, targets):
        ins_tag = f"{{{self.namespaces['w']}}}ins"
        del_tag = f"{{{self.namespaces['w']}}}del"

        for parent in root.iter():
            to_remove = []
            for child in parent:
                if child.tag == ins_tag and child in targets:
                    to_remove.append(child)
            for elem in to_remove:
                parent.remove(elem)

        deltext_tag = f"{{{self.namespaces['w']}}}delText"
        t_tag = f"{{{self.namespaces['w']}}}t"

        for parent in root.iter():
            to_process = []
            for child in parent:
                if child.tag == del_tag and child in targets:
                    to_process.append((child, list(parent).index(child)))

            for del_elem, del_index in reversed(to_process):
                for elem in del_elem.iter():
                    if elem.tag == deltext_tag:
                        elem.tag = t_tag

                for child in reversed(list(del_elem)):
                    parent.insert(del_index, child)
                parent.remove(del_elem)

    def _paragraph_text(self, p_elem):
        t_tag = f"{{{self.namespaces['w']}}}t"
        return "".join(self._rendered_text(t) for t in p_elem.findall(f".//{t_tag}"))

    def _extract_text_content(self, root):
        p_tag = f"{{{self.namespaces['w']}}}p"

        paragraphs = []
        for p_elem in root.findall(f".//{p_tag}"):
            paragraph_text = self._paragraph_text(p_elem)
            if paragraph_text:
                paragraphs.append(paragraph_text)

        return "\n".join(paragraphs)


if __name__ == "__main__":
    raise RuntimeError("This module should not be run directly.")
