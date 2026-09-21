#!/usr/bin/env python3

"""
epub_cleaner.py

Two-stage EPUB cleaner/minimizer.

Stage 1:
    Normalize XHTML namespace declarations without parsing/re-serializing:
      * Keep xmlns="http://www.w3.org/1999/xhtml" on the root <html>.
      * Remove the same xmlns attribute from every descendant element.
      * Preserve every other byte exactly.

Stage 2:
    Find the problematic XHTML file (unless --html-path is supplied), then
    minimize Kobo spans using Tree-sitter byte offsets.

    A Kobo span is an element like:
        <span class="koboSpan" id="kobo.123.1">...</span>

    Removing a Kobo span means removing ONLY its opening and closing tags.
    The contents remain untouched.

The minimizer searches for a 1-minimal set of Kobo spans whose wrapper-tag
removal makes the EPUB load quickly enough for the configured reader.

Dependencies:
    psutil
    tree-sitter
    tree-sitter-html

Example:
    python3 epub_cleaner.py broken.epub

    python3 epub_cleaner.py broken.epub \
        --html-path OEBPS/appendix-001.xhtml

    python3 epub_cleaner.py broken.epub \
        --epub-reader okular \
        --timeout 5 \
        --idle-threshold 5 \
        --idle-samples 3
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

try:
    import psutil
except ImportError:
    print("ERROR: missing Python package: psutil", file=sys.stderr)
    print("Install with: python3 -m pip install psutil", file=sys.stderr)
    sys.exit(1)

try:
    from tree_sitter import Language, Parser
    import tree_sitter_html
except ImportError:
    print(
        "ERROR: missing Tree-sitter packages.\n"
        "Install with:\n"
        "    python3 -m pip install tree-sitter tree-sitter-html",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

XHTML_EXTENSIONS = {
    ".html",
    ".htm",
    ".xhtml",
    ".xht",
}

XHTML_NAMESPACE = b"http://www.w3.org/1999/xhtml"

DEFAULT_TIMEOUT = 5.0
DEFAULT_INTERVAL = 0.05
DEFAULT_IDLE_THRESHOLD = 5.0
DEFAULT_IDLE_SAMPLES = 3


# ---------------------------------------------------------------------------
# Tree-sitter setup
# ---------------------------------------------------------------------------

def make_parser() -> Parser:
    """
    Create a Tree-sitter HTML parser.

    Supports both newer and older Python Tree-sitter APIs.
    """
    language = Language(tree_sitter_html.language())

    try:
        # Current API.
        return Parser(language)
    except TypeError:
        # Older API.
        parser = Parser()
        parser.set_language(language)
        return parser


# ---------------------------------------------------------------------------
# Tree-sitter helpers
# ---------------------------------------------------------------------------

def walk(node):
    """Yield node and all descendants."""
    yield node
    for child in node.children:
        yield from walk(child)


def descendants_of_type(node, node_type: str):
    for child in walk(node):
        if child.type == node_type:
            yield child


def direct_child(node, node_type: str):
    for child in node.children:
        if child.type == node_type:
            return child
    return None


def node_text(data: bytes, node) -> bytes:
    return data[node.start_byte:node.end_byte]


def get_tag_name(data: bytes, start_tag) -> bytes | None:
    """
    Get the tag name from a Tree-sitter start_tag.

    We primarily use the CST, but tolerate grammar/API variations by looking
    for a tag_name descendant.
    """
    tag_name = direct_child(start_tag, "tag_name")

    if tag_name is None:
        for child in descendants_of_type(start_tag, "tag_name"):
            tag_name = child
            break

    if tag_name is None:
        return None

    return node_text(data, tag_name).strip().lower()


def get_attribute_nodes(start_tag):
    """
    Yield attribute nodes belonging to this start tag.

    In tree-sitter-html these are normally direct children, but searching
    descendants makes this tolerant of small grammar differences.
    """
    for child in start_tag.children:
        if child.type == "attribute":
            yield child


def parse_attribute_name_and_value_zzzzzzzzzz(
        data: bytes,
        attribute,
    ) -> tuple[bytes, bytes | None]:
    """
    Extract an attribute's name/value from its CST node.

    The structure of attribute-value nodes differs slightly between grammar
    versions, so the attribute itself is also inspected as raw bytes.

    This is NOT used to rewrite HTML. It is only used to identify attributes.
    """
    raw = node_text(data, attribute).strip()

    # Attribute name: everything before whitespace or '='.
    m = re.match(rb"([A-Za-z_:][A-Za-z0-9_.:-]*)", raw)
    if not m:
        return b"", None

    name = m.group(1).lower()

    rest = raw[m.end():].lstrip()

    if not rest.startswith(b"="):
        return name, None

    rest = rest[1:].lstrip()

    if len(rest) >= 2 and rest[:1] in (b'"', b"'"):
        quote = rest[:1]
        end = rest.find(quote, 1)
        if end >= 0:
            return name, rest[1:end]

    return name, rest


def parse_attribute_name_and_value(
        data: bytes,
        attribute,
    ) -> tuple[bytes, bytes | None]:
    """
    Extract an HTML attribute from a tree-sitter 'attribute' node.

    The CST tells us that this is an actual attribute. We then inspect only
    the bytes belonging to that attribute.

    Examples:

        class="koboSpan"
            -> (b"class", b"koboSpan")

        id="kobo.1.1"
            -> (b"id", b"kobo.1.1")

        xmlns="http://www.w3.org/1999/xhtml"
            -> (b"xmlns", b"http://www.w3.org/1999/xhtml")
    """
    raw = data[attribute.start_byte:attribute.end_byte]

    # Find the actual attribute name.
    m = re.match(
        rb"([A-Za-z_:][A-Za-z0-9_.:-]*)",
        raw,
    )

    if not m:
        return b"", None

    name = m.group(1).lower()

    # Find '='.
    pos = m.end()

    while pos < len(raw) and raw[pos:pos + 1] in (
        b" ",
        b"\t",
        b"\r",
        b"\n",
    ):
        pos += 1

    if pos >= len(raw) or raw[pos:pos + 1] != b"=":
        return name, None

    pos += 1

    while pos < len(raw) and raw[pos:pos + 1] in (
        b" ",
        b"\t",
        b"\r",
        b"\n",
    ):
        pos += 1

    if pos >= len(raw):
        return name, None

    # Quoted attribute.
    if raw[pos:pos + 1] in (b'"', b"'"):
        quote = raw[pos:pos + 1]
        value_start = pos + 1
        value_end = raw.find(quote, value_start)

        if value_end == -1:
            return name, None

        return name, raw[value_start:value_end]

    # Unquoted attribute.
    value = raw[pos:].split(
        None,
        1,
    )[0]

    return name, value


def get_element_start_and_end_tags(data: bytes, element):
    start_tag = None
    end_tag = None

    for child in element.children:
        if child.type == "start_tag":
            start_tag = child
        elif child.type == "end_tag":
            end_tag = child

    return start_tag, end_tag


def is_html_root_element(data: bytes, element) -> bool:
    """
    True if this is the document's root <html> element.

    We deliberately use the CST hierarchy rather than merely looking for the
    first '<html' string.
    """
    start_tag, _ = get_element_start_and_end_tags(data, element)

    if start_tag is None:
        return False

    name = get_tag_name(data, start_tag)
    if name != b"html":
        return False

    # An HTML element whose parent is the document/root node is the root.
    parent = element.parent
    if parent is None:
        return True

    # Depending on grammar version, the root document may be "document".
    if parent.type in ("document", "fragment"):
        return True

    return False


def element_has_class(data: bytes, start_tag, wanted: bytes) -> bool:
    """
    Check the parsed class attribute for a class token.

    We inspect only actual CST attribute nodes, not arbitrary text in the
    element.
    """
    for attribute in get_attribute_nodes(start_tag):
        name, value = parse_attribute_name_and_value(data, attribute)

        if name != b"class" or value is None:
            continue

        classes = value.split()
        if wanted in classes:
            return True

    return False


def get_attribute_value(data: bytes, start_tag, wanted_name: bytes):
    for attribute in get_attribute_nodes(start_tag):
        name, value = parse_attribute_name_and_value(data, attribute)

        if name == wanted_name:
            return value

    return None


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KoboSpan:
    index: int
    start_tag_start: int
    start_tag_end: int
    end_tag_start: int
    end_tag_end: int
    element_start: int
    element_end: int
    span_id: str
    id_value: bytes # or str?
    class_value: bytes # or str?
    node: "TreeSitterNode"


@dataclass
class BenchmarkResult:
    good: bool
    peak_cpu: float
    elapsed: float
    target_pid: int | None


# ---------------------------------------------------------------------------
# XHTML parsing
# ---------------------------------------------------------------------------

def parse_tree(data: bytes):
    parser = make_parser()
    return parser.parse(data)


def find_elements(data: bytes):
    tree = parse_tree(data)

    for node in walk(tree.root_node):
        if node.type == "element":
            yield node


# ---------------------------------------------------------------------------
# Stage 1: namespace normalization
# ---------------------------------------------------------------------------

def namespace_removal_edits_zzzzzzzzzzzzzzz(data: bytes) -> list[tuple[int, int]]:
    """
    Find redundant XHTML namespace declarations.

    Keep xmlns="http://www.w3.org/1999/xhtml" on root <html>.
    Remove it from every descendant element.

    Only the exact attribute bytes are removed. No serialization occurs.
    """
    edits: list[tuple[int, int]] = []

    for element in find_elements(data):
        start_tag, _ = get_element_start_and_end_tags(data, element)

        if start_tag is None:
            continue

        if is_html_root_element(data, element):
            continue

        for attribute in get_attribute_nodes(start_tag):
            name, value = parse_attribute_name_and_value(data, attribute)

            if name != b"xmlns":
                continue

            if value != XHTML_NAMESPACE:
                continue

            # Remove the whitespace immediately preceding the attribute too.
            #
            # Example:
            #   <div xmlns="http://www.w3.org/1999/xhtml">
            #
            # becomes:
            #   <div>
            #
            # while:
            #   <div class="x" xmlns="http://www.w3.org/1999/xhtml">
            #
            # becomes:
            #   <div class="x">
            start = attribute.start_byte

            while start > start_tag.start_byte and data[start - 1:start] in (
                b" ",
                b"\t",
                b"\r",
                b"\n",
            ):
                start -= 1

            edits.append((start, attribute.end_byte))

    return merge_ranges(edits)


def is_root_html_element(data: bytes, element) -> bool:
    """
    Identify the document's actual root <html> element.

    With tree-sitter-html the document looks like:

        document
          element
            start_tag
              tag_name = html

    We therefore explicitly require the element's parent to be the
    document node.
    """
    if element.parent is None:
        return False

    if element.parent.type != "document":
        return False

    start_tag, _ = get_element_start_and_end_tags(data, element)

    if start_tag is None:
        return False

    tag_name = get_tag_name(data, start_tag)

    return tag_name == b"html"


def namespace_removal_edits(data: bytes) -> list[tuple[int, int]]:
    """
    Remove xmlns="http://www.w3.org/1999/xhtml" from every element except
    the root <html> element.

    No parsing/re-serialization takes place. Only the exact attribute bytes
    (plus the whitespace immediately before them) are removed.
    """
    edits = []

    element_count = 0
    root_html_count = 0
    xmlns_count = 0
    descendant_xmlns_count = 0

    for element in find_elements(data):
        element_count += 1

        start_tag, _ = get_element_start_and_end_tags(data, element)

        if start_tag is None:
            continue

        tag_name = get_tag_name(data, start_tag)

        is_root = is_root_html_element(data, element)

        if is_root:
            root_html_count += 1

        for attribute in get_attribute_nodes(start_tag):
            name, value = parse_attribute_name_and_value(
                data,
                attribute,
            )

            if name != b"xmlns":
                continue

            xmlns_count += 1

            if value != XHTML_NAMESPACE:
                continue

            if is_root:
                # The one xmlns on the root <html> stays.
                continue

            descendant_xmlns_count += 1

            # Remove whitespace immediately before the attribute.
            #
            # <div xmlns="...">
            #     ^^^^^^^^^^^
            #
            # becomes:
            #
            # <div>
            #
            # If the xmlns is not the first attribute:
            #
            # <div class="x" xmlns="...">
            #
            # becomes:
            #
            # <div class="x">
            start = attribute.start_byte

            while start > start_tag.start_byte:
                previous = data[start - 1:start]

                if previous in (b" ", b"\t", b"\r", b"\n"):
                    start -= 1
                else:
                    break

            edits.append(
                (
                    start,
                    attribute.end_byte,
                )
            )

            print(
                f"    namespace removal: "
                f"<{tag_name.decode(errors='replace')}> "
                f"bytes {start}-{attribute.end_byte}"
            )

    print(
        f"    DEBUG: elements={element_count}, "
        f"root-html={root_html_count}, "
        f"xmlns={xmlns_count}, "
        f"descendant-xhtml-xmlns={descendant_xmlns_count}"
    )

    return merge_ranges(edits)


def merge_ranges(ranges: Iterable[tuple[int, int]]):
    """
    Merge overlapping/adjacent byte ranges.
    """
    sorted_ranges = sorted(ranges)

    if not sorted_ranges:
        return []

    result = [sorted_ranges[0]]

    for start, end in sorted_ranges[1:]:
        old_start, old_end = result[-1]

        if start <= old_end:
            result[-1] = (old_start, max(old_end, end))
        else:
            result.append((start, end))

    return result


def apply_deletions(data: bytes, ranges: Iterable[tuple[int, int]]) -> bytes:
    """
    Delete byte ranges without reserialization.

    Ranges are applied from right to left so offsets remain valid.
    """
    result = data

    for start, end in sorted(ranges, reverse=True):
        result = result[:start] + result[end:]

    return result


def normalize_xhtml_bytes(data: bytes) -> tuple[bytes, int]:
    edits = namespace_removal_edits(data)
    return apply_deletions(data, edits), len(edits)


def normalize_directory(directory: Path) -> int:
    """
    Normalize every XHTML/HTML file in an unpacked EPUB directory.
    """
    total = 0
    files = 0

    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue

        if path.suffix.lower() not in XHTML_EXTENSIONS:
            continue

        data = path.read_bytes()
        normalized, count = normalize_xhtml_bytes(data)

        if normalized != data:
            path.write_bytes(normalized)

        total += count
        files += 1

    print()
    print("=" * 72)
    print("STAGE 1: XHTML NAMESPACE NORMALIZATION")
    print("=" * 72)
    print(f"HTML/XHTML files scanned: {files}")
    print(f"Redundant xmlns attributes removed: {total}")

    return total


# ---------------------------------------------------------------------------
# EPUB unpacking/packing
# ---------------------------------------------------------------------------

def unpack_epub(epub_path: Path, directory: Path):
    directory.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(epub_path, "r") as z:
        z.extractall(directory)


def pack_epub(directory: Path, output_epub: Path):
    output_epub.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(
        output_epub,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as z:
        mimetype = directory / "mimetype"

        # EPUB requires mimetype to be first and uncompressed.
        if mimetype.exists():
            z.write(mimetype, "mimetype", compress_type=zipfile.ZIP_STORED)

        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue

            rel = path.relative_to(directory)

            if rel.as_posix() == "mimetype":
                continue

            z.write(path, rel.as_posix(), compress_type=zipfile.ZIP_DEFLATED)


# ---------------------------------------------------------------------------
# EPUB file isolation
# ---------------------------------------------------------------------------

BODY_OPEN_RE = re.compile(rb"<body\b[^>]*>", re.I)
BODY_CLOSE_RE = re.compile(rb"</body\s*>", re.I)


def blank_body(data: bytes) -> bytes:
    """
    Remove the contents of <body>, retaining the body tags themselves.

    This is only used during automatic XHTML-file isolation.
    """
    m_open = BODY_OPEN_RE.search(data)
    if not m_open:
        return data

    m_close = BODY_CLOSE_RE.search(data, m_open.end())
    if not m_close:
        return data

    return (
        data[:m_open.end()]
        + b"\n"
        + data[m_close.start():]
    )


def isolate_html_files(
    normalized_directory: Path,
    candidate_relative_path: Path,
    test_directory: Path,
):
    """
    Copy normalized EPUB and blank every HTML/XHTML file except candidate.
    """
    if test_directory.exists():
        shutil.rmtree(test_directory)

    shutil.copytree(normalized_directory, test_directory)

    candidate = test_directory / candidate_relative_path

    if not candidate.exists():
        raise FileNotFoundError(candidate)

    for path in sorted(test_directory.rglob("*")):
        if not path.is_file():
            continue

        if path.suffix.lower() not in XHTML_EXTENSIONS:
            continue

        if path.resolve() == candidate.resolve():
            continue

        path.write_bytes(blank_body(path.read_bytes()))


# ---------------------------------------------------------------------------
# Process discovery / benchmark
# ---------------------------------------------------------------------------

def process_snapshot():
    result = {}

    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        try:
            info = proc.info
            result[info["pid"]] = info
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return result


def process_matches_reader(info, reader: str) -> bool:
    name = (info.get("name") or "").lower()
    cmdline = info.get("cmdline") or []

    reader_base = Path(reader).name.lower()

    # NixOS Okular wrapper commonly looks like:
    #     .okular-wrapped
    if reader_base == "okular":
        if "okular" in name:
            return True

        if any("okular" in Path(x).name.lower() for x in cmdline):
            return True

    if reader_base in name:
        return True

    if any(reader_base in Path(x).name.lower() for x in cmdline):
        return True

    return False


def find_new_reader_process(
    before,
    reader: str,
    launcher_pid: int,
    deadline: float,
):
    """
    Find the actual reader process spawned by the launcher.

    On NixOS this intentionally prefers .okular-wrapped over the shell wrapper.
    """
    while time.monotonic() < deadline:
        candidates = []

        for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
            try:
                info = proc.info

                pid = info["pid"]

                if pid in before:
                    continue

                if pid == launcher_pid:
                    continue

                if not process_matches_reader(info, reader):
                    continue

                candidates.append(info)

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if candidates:
            # Prefer a process whose parent is the launcher.
            direct = [
                x for x in candidates
                if x.get("ppid") == launcher_pid
            ]

            chosen = direct[0] if direct else candidates[0]

            return chosen["pid"]

        time.sleep(0.01)

    return None


def kill_pid(pid: int | None):
    if pid is None:
        return

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    try:
        proc.terminate()
        proc.wait(timeout=0.5)
        return
    except (psutil.NoSuchProcess, psutil.TimeoutExpired, psutil.AccessDenied):
        pass

    try:
        proc.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def benchmark_epub(
    epub_path: Path,
    reader: str,
    timeout: float,
    interval: float,
    idle_threshold: float,
    idle_samples: int,
    verbose: bool = True,
) -> BenchmarkResult:
    """
    Launch EPUB reader and classify it as GOOD/BAD.

    GOOD:
        CPU falls to <= idle_threshold for idle_samples consecutive samples.

    BAD:
        CPU never settles to idle before timeout.

    No fixed warmup is used. CPU monitoring begins immediately after the
    actual reader process is identified.
    """
    before = process_snapshot()

    started = time.monotonic()

    try:
        launcher = subprocess.Popen(
            [reader, str(epub_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"EPUB reader not found: {reader!r}"
        )

    target_pid = None

    # Give process discovery up to 1 second, but never beyond benchmark
    # timeout.
    discovery_deadline = min(
        started + timeout,
        started + 1.0,
    )

    target_pid = find_new_reader_process(
        before,
        reader,
        launcher.pid,
        discovery_deadline,
    )

    if target_pid is None:
        # Fallback: monitor launcher if we cannot identify the wrapped reader.
        target_pid = launcher.pid

    try:
        proc = psutil.Process(target_pid)
    except psutil.NoSuchProcess:
        elapsed = time.monotonic() - started
        kill_pid(launcher.pid)
        return BenchmarkResult(
            good=True,
            peak_cpu=0.0,
            elapsed=elapsed,
            target_pid=target_pid,
        )

    # Prime cpu_percent immediately. No warmup.
    try:
        proc.cpu_percent(None)
    except psutil.NoSuchProcess:
        kill_pid(launcher.pid)
        return BenchmarkResult(
            good=True,
            peak_cpu=0.0,
            elapsed=time.monotonic() - started,
            target_pid=target_pid,
        )

    peak_cpu = 0.0
    idle_count = 0
    good = False

    deadline = started + timeout

    if verbose:
        print(f"    monitoring PID {target_pid}")

    while time.monotonic() < deadline:
        time.sleep(interval)

        try:
            if not proc.is_running():
                good = True
                break

            cpu = proc.cpu_percent(None)
            peak_cpu = max(peak_cpu, cpu)

            if verbose:
                print(f"    CPU: {cpu:6.1f}%")

            if cpu <= idle_threshold:
                idle_count += 1

                if idle_count >= idle_samples:
                    good = True
                    break
            else:
                idle_count = 0

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            good = True
            break

    elapsed = time.monotonic() - started

    # Kill only the actual reader and launcher directly.
    # Do NOT kill process groups: on some systems this can kill unrelated
    # processes.
    if target_pid != launcher.pid:
        kill_pid(target_pid)

    kill_pid(launcher.pid)

    return BenchmarkResult(
        good=good,
        peak_cpu=peak_cpu,
        elapsed=elapsed,
        target_pid=target_pid,
    )


# ---------------------------------------------------------------------------
# EPUB test wrapper
# ---------------------------------------------------------------------------

def pack_and_benchmark(
    directory: Path,
    epub_path: Path,
    reader: str,
    timeout: float,
    interval: float,
    idle_threshold: float,
    idle_samples: int,
    label: str,
    verbose: bool = True,
) -> BenchmarkResult:
    pack_epub(directory, epub_path)

    print()
    print(f"[TEST] {label}")

    result = benchmark_epub(
        epub_path,
        reader,
        timeout,
        interval,
        idle_threshold,
        idle_samples,
        verbose=verbose,
    )

    state = "GOOD" if result.good else "BAD"

    print(
        f"    => {state} "
        f"(peak {result.peak_cpu:.1f}%, "
        f"{result.elapsed:.2f}s)"
    )

    return result


# ---------------------------------------------------------------------------
# Find Kobo spans
# ---------------------------------------------------------------------------

def find_kobo_spans(data: bytes) -> list[KoboSpan]:
    """
    Locate real <span class="... koboSpan ..."> elements using Tree-sitter.

    Only elements with both opening and closing tags are returned.
    """
    result: list[KoboSpan] = []

    for element in find_elements(data):
        start_tag, end_tag = get_element_start_and_end_tags(data, element)

        if start_tag is None or end_tag is None:
            continue

        tag_name = get_tag_name(data, start_tag)

        if tag_name != b"span":
            continue

        if not element_has_class(data, start_tag, b"koboSpan"):
            continue

        span_id = get_attribute_value(data, start_tag, b"id")

        if span_id is None:
            span_id_text = "(no id)"
        else:
            span_id_text = span_id.decode("utf-8", errors="replace")

        class_value = None
        id_value = None
        for attr in start_tag.children:
            if attr.type != "attribute":
                continue
            name, value = parse_attribute_name_and_value(data, attr)
            if name.lower() == b"class":
                class_value = value
            elif name.lower() == b"id":
                id_value = value
        # if class_value is None:
        #     continue

        result.append(
            KoboSpan(
                index=len(result),
                start_tag_start=start_tag.start_byte,
                start_tag_end=start_tag.end_byte,
                end_tag_start=end_tag.start_byte,
                end_tag_end=end_tag.end_byte,
                element_start=element.start_byte,
                element_end=element.end_byte,
                span_id=span_id_text,

                node=element,
                id_value=id_value,
                class_value=class_value,
            )
        )

    return result


def find_kobo_spans_zzzzzzzzz(data: bytes) -> list[KoboSpan]:
    """
    Find <span> elements whose parsed class attribute contains koboSpan.
    """
    result = []

    element_count = 0
    span_count = 0
    kobo_count = 0

    for element in find_elements(data):
        element_count += 1

        start_tag, end_tag = get_element_start_and_end_tags(
            data,
            element,
        )

        if start_tag is None:
            continue

        tag_name = get_tag_name(data, start_tag)

        if tag_name != b"span":
            continue

        span_count += 1

        attributes = []

        for attribute in get_attribute_nodes(start_tag):
            name, value = parse_attribute_name_and_value(
                data,
                attribute,
            )

            attributes.append(
                (
                    name,
                    value,
                    attribute.start_byte,
                    attribute.end_byte,
                )
            )

        class_value = None
        span_id_value = None

        for name, value, start, end in attributes:
            if name == b"class":
                class_value = value

            if name == b"id":
                span_id_value = value

        if class_value is None:
            continue

        classes = class_value.split()

        if b"koboSpan" not in classes:
            continue

        kobo_count += 1

        # A span we intend to remove must have a closing tag.
        if end_tag is None:
            print(
                f"    WARNING: Kobo span without end tag at "
                f"{element.start_byte}-{element.end_byte}"
            )
            continue

        span_id = (
            span_id_value.decode(
                "utf-8",
                errors="replace",
            )
            if span_id_value is not None
            else "(no id)"
        )

        span = KoboSpan(
            index=len(result),
            start_tag_start=start_tag.start_byte,
            start_tag_end=start_tag.end_byte,
            end_tag_start=end_tag.start_byte,
            end_tag_end=end_tag.end_byte,
            element_start=element.start_byte,
            element_end=element.end_byte,
            span_id=span_id,
        )

        result.append(span)

        print(
            f"    KOBO [{span.index:4d}] "
            f"id={span.span_id} "
            f"element={span.element_start}-{span.element_end} "
            f"open={span.start_tag_start}-{span.start_tag_end} "
            f"close={span.end_tag_start}-{span.end_tag_end}"
        )

    print(
        f"    DEBUG: elements={element_count}, "
        f"span-elements={span_count}, "
        f"koboSpan={kobo_count}"
    )

    return result


def find_kobo_spans_zzzzzzzzz(data: bytes) -> list[KoboSpan]:
    result = []

    element_count = 0
    span_count = 0
    kobo_count = 0

    for element in find_elements(data):
        element_count += 1

        start_tag, end_tag = get_element_start_and_end_tags(
            data,
            element,
        )

        if start_tag is None:
            continue

        tag_name = get_tag_name(data, start_tag)

        if tag_name != b"span":
            continue

        span_count += 1

        print()
        print(
            f"DEBUG SPAN #{span_count}: "
            f"bytes {element.start_byte}-{element.end_byte}"
        )

        print(
            "    START TAG:",
            repr(data[start_tag.start_byte:start_tag.end_byte])
        )

        attributes = []

        for attribute in get_attribute_nodes(start_tag):
            raw = data[
                attribute.start_byte:
                attribute.end_byte
            ]

            name, value = parse_attribute_name_and_value(
                data,
                attribute,
            )

            print(
                f"    ATTRIBUTE: "
                f"raw={raw!r} "
                f"name={name!r} "
                f"value={value!r}"
            )

            attributes.append((name, value))

        class_value = None
        span_id_value = None

        for name, value in attributes:
            if name == b"class":
                class_value = value

            elif name == b"id":
                span_id_value = value

        print(
            f"    CLASS VALUE: {class_value!r}"
        )

        print(
            f"    ID VALUE: {span_id_value!r}"
        )

        if class_value is None:
            print("    => NO CLASS ATTRIBUTE")
            continue

        classes = class_value.split()

        print(
            f"    CLASS TOKENS: {classes!r}"
        )

        if b"koboSpan" not in classes:
            print("    => NOT A KOBO SPAN")
            continue

        print("    => *** KOBO SPAN ***")

        kobo_count += 1

        if end_tag is None:
            print(
                "    WARNING: no closing tag"
            )
            continue

        span_id = (
            span_id_value.decode(
                "utf-8",
                errors="replace",
            )
            if span_id_value is not None
            else "(no id)"
        )

        span = KoboSpan(
            index=len(result),
            start_tag_start=start_tag.start_byte,
            start_tag_end=start_tag.end_byte,
            end_tag_start=end_tag.start_byte,
            end_tag_end=end_tag.end_byte,
            element_start=element.start_byte,
            element_end=element.end_byte,
            span_id=span_id,
        )

        result.append(span)

        print(
            f"    RECORDED: "
            f"[{span.index}] "
            f"{span.span_id}"
        )

    print()
    print(
        f"DEBUG SUMMARY: "
        f"elements={element_count}, "
        f"span-elements={span_count}, "
        f"koboSpan={kobo_count}"
    )

    return result


# FIXME remove this alias
parse_html = parse_tree


def find_kobo_spans_zzzzzzzzzz(data):
    tree = parse_html(data)
    spans = []

    if 1:
        # debug
        print("debug: searching bytestring 'koboSpan' in raw bytes")
        found = 0
        for node in walk(tree.root_node):
            if node.type == "attribute":
                raw = data[node.start_byte:node.end_byte]
                if b"koboSpan" in raw:
                    found += 1
                    print(
                        "FOUND KOBO BYTESTRING IN ATTRIBUTE:",
                        node.start_byte,
                        node.end_byte,
                        repr(raw),
                    )
        print(f"debug: searching bytestring 'koboSpan' in raw bytes: found={found}")

    for node in walk(tree.root_node):
        if node.type != "element":
            continue

        tag = get_tag_name(data, node)
        if tag.lower() != b"span":
            continue

        start_tag = direct_child(node, "start_tag")
        if start_tag is None:
            continue

        class_value = None
        id_value = None

        for attr in start_tag.children:
            if attr.type != "attribute":
                continue

            name, value = parse_attribute_name_and_value(data, attr)

            if name.lower() == b"class":
                class_value = value

            elif name.lower() == b"id":
                id_value = value

        if class_value is None:
            continue

        tokens = class_value.split()

        if b"koboSpan" in tokens:
            spans.append(
                # FIXME TypeError: KoboSpan.__init__() got an unexpected keyword argument 'node'
                KoboSpan(
                    node=node,
                    start_tag=start_tag,
                    class_value=class_value,
                    id_value=id_value,
                )
            )
            # expected:
            r'''
            result.append(
                KoboSpan(
                    index=len(result),
                    start_tag_start=start_tag.start_byte,
                    start_tag_end=start_tag.end_byte,
                    end_tag_start=end_tag.start_byte,
                    end_tag_end=end_tag.end_byte,
                    element_start=element.start_byte,
                    element_end=element.end_byte,
                    span_id=span_id_text,
                )
            )
            '''

    return spans


def removal_ranges_for_spans(
        spans: list[KoboSpan],
        indices: set[int],
    ):
    ranges = []

    for i in indices:
        span = spans[i]

        ranges.append(
            (span.start_tag_start, span.start_tag_end)
        )

        ranges.append(
            (span.end_tag_start, span.end_tag_end)
        )

    return merge_ranges(ranges)


def remove_kobo_spans(
    data: bytes,
    spans: list[KoboSpan],
    indices: set[int],
) -> bytes:
    return apply_deletions(
        data,
        removal_ranges_for_spans(spans, indices),
    )


# ---------------------------------------------------------------------------
# Delta debugging / minimization
# ---------------------------------------------------------------------------

def partition(items: list[int], parts: int):
    """
    Split items into approximately equal contiguous groups.
    """
    n = len(items)
    parts = max(1, min(parts, n))

    groups = []

    base = n // parts
    remainder = n % parts

    pos = 0

    for i in range(parts):
        size = base + (1 if i < remainder else 0)

        if size:
            groups.append(items[pos:pos + size])

        pos += size

    return groups


def minimize_kobo_spans(
        normalized_directory: Path,
        target_relative_path: Path,
        output_dir: Path,
        reader: str,
        timeout: float,
        interval: float,
        idle_threshold: float,
        idle_samples: int,
        max_tests: int | None,
        keep_tests: bool,
    ):
    """
    Find a 1-minimal set of Kobo spans that must be removed.

    Algorithm:

        removed = ALL Kobo spans

        Verify:
            all spans removed => GOOD

        Then repeatedly try to RESTORE groups of spans.

        If restoring a group still gives GOOD:
            keep the spans restored.

        If restoring a group gives BAD:
            at least one span in that group is required.

        Increase granularity until individual spans are tested.

    Result:
        No individual removed span can be restored without making the EPUB BAD.

    This is 1-minimal, not guaranteed globally minimum under arbitrary
    interactions between spans.
    """
    print(f"minimize_kobo_spans: normalized_directory={normalized_directory}")

    target_path = normalized_directory / target_relative_path
    print(f"minimize_kobo_spans: target_path={target_path}")

    if not target_path.exists():
        raise FileNotFoundError(target_path)

    print("minimize_kobo_spans: find_kobo_spans")
    baseline_data = target_path.read_bytes()
    spans = find_kobo_spans(baseline_data)

    if 1:
        # debug
        # kobo_spans = find_kobo_spans(data)
        kobo_spans = spans
        data = baseline_data
        print(f"Total <span> elements: {sum(1 for n in walk(parse_html(data).root_node) if n.type == 'element' and get_tag_name(data, n).lower() == b'span')}")
        print(f"Kobo spans found: {len(kobo_spans)}")
        for i, span in enumerate(kobo_spans[:20], 1):
            print(
                f"  KOBO #{i}: "
                # f"id={span.id_value!r}, "
                f"id={span.span_id!r}, "
                f"class={span.class_value!r}, "
                f"bytes={span.node.start_byte}-{span.node.end_byte}"
            )

    print()
    print("=" * 72)
    print("STAGE 2: KOBO SPAN ANALYSIS")
    print("=" * 72)
    print(f"Target: {target_relative_path}")
    print(f"Kobo spans found: {len(spans)}")

    if not spans:
        print("ERROR: No koboSpan elements were found.")
        return None

    for span in spans:
        print(
            f"    [{span.index:4d}] "
            f"{span.span_id} "
            f"bytes {span.element_start}-{span.element_end}"
        )

    # Keep a clean copy of the normalized baseline.
    normalized_epub = output_dir / "NORMALIZED.epub"

    print()
    print("Testing normalized baseline...")

    pack_epub(normalized_directory, normalized_epub)

    baseline_result = benchmark_epub(
        normalized_epub,
        reader,
        timeout,
        interval,
        idle_threshold,
        idle_samples,
    )

    print(
        f"    => {'GOOD' if baseline_result.good else 'BAD'} "
        f"(peak {baseline_result.peak_cpu:.1f}%, "
        f"{baseline_result.elapsed:.2f}s)"
    )

    if baseline_result.good:
        print()
        print(
            "Namespace normalization alone fixed the EPUB."
        )
        return {
            "spans": spans,
            "removed": set(),
            "final_directory": normalized_directory,
            "tests": 1,
        }

    # ------------------------------------------------------------------
    # First prove that removing ALL Kobo span wrappers fixes it.
    # ------------------------------------------------------------------

    all_indices = set(range(len(spans)))

    all_removed_data = remove_kobo_spans(
        baseline_data,
        spans,
        all_indices,
    )

    all_removed_directory = output_dir / "_all_kobo_removed"

    if all_removed_directory.exists():
        shutil.rmtree(all_removed_directory)

    shutil.copytree(normalized_directory, all_removed_directory)

    (all_removed_directory / target_relative_path).write_bytes(
        all_removed_data
    )

    all_removed_epub = output_dir / "_all_kobo_removed.epub"

    print()
    print("Testing EPUB with ALL Kobo span wrappers removed...")

    result = pack_and_benchmark(
        all_removed_directory,
        all_removed_epub,
        reader,
        timeout,
        interval,
        idle_threshold,
        idle_samples,
        "all Kobo spans removed",
    )

    tests = 2

    if not result.good:
        print()
        print(
            "ERROR: Removing all Kobo span wrappers does not make the "
            "EPUB GOOD."
        )
        print(
            "Therefore the problem is not caused solely by the Kobo "
            "span wrappers."
        )

        if not keep_tests:
            shutil.rmtree(all_removed_directory, ignore_errors=True)
            all_removed_epub.unlink(missing_ok=True)

        return None

    # ------------------------------------------------------------------
    # Delta-debug by RESTORING groups.
    # ------------------------------------------------------------------

    removed = set(all_indices)

    # Current granularity.
    granularity = 2

    print()
    print("-" * 72)
    print("MINIMIZING KOBO SPAN REMOVALS")
    print("-" * 72)

    while len(removed) >= 1:
        if max_tests is not None and tests >= max_tests:
            print()
            print(f"Reached --max-tests={max_tests}.")
            break

        ordered_removed = sorted(removed)

        if not ordered_removed:
            break

        granularity = min(
            granularity,
            len(ordered_removed),
        )

        groups = partition(
            ordered_removed,
            granularity,
        )

        progress = False

        print()
        print(
            f"Trying to restore {len(groups)} groups "
            f"(granularity={granularity}, "
            f"currently removed={len(removed)})"
        )

        for group_number, group in enumerate(groups, 1):
            if max_tests is not None and tests >= max_tests:
                break

            group_set = set(group)

            # Candidate: restore this group.
            candidate_removed = removed - group_set

            candidate_data = remove_kobo_spans(
                baseline_data,
                spans,
                candidate_removed,
            )

            test_directory = output_dir / (
                f"_test_{tests:05d}"
            )

            if test_directory.exists():
                shutil.rmtree(test_directory)

            shutil.copytree(
                normalized_directory,
                test_directory,
            )

            (
                test_directory / target_relative_path
            ).write_bytes(candidate_data)

            test_epub = output_dir / (
                f"_test_{tests:05d}.epub"
            )

            result = pack_and_benchmark(
                test_directory,
                test_epub,
                reader,
                timeout,
                interval,
                idle_threshold,
                idle_samples,
                (
                    f"restore group {group_number}/{len(groups)} "
                    f"({len(group)} spans)"
                ),
            )

            tests += 1

            if result.good:
                # Excellent: these spans are not necessary to remove.
                removed = candidate_removed
                progress = True

                print(
                    f"    RESTORED {len(group)} spans; "
                    f"{len(removed)} remain removed"
                )

            else:
                print(
                    f"    KEEP REMOVING this group; "
                    f"{len(group)} spans still implicated"
                )

            if not keep_tests:
                shutil.rmtree(
                    test_directory,
                    ignore_errors=True,
                )
                test_epub.unlink(missing_ok=True)

        if not removed:
            print()
            print("All Kobo spans can be restored. No Kobo span is required.")
            break

        if progress:
            # Continue at same granularity so we can exploit the newly
            # enlarged search space.
            granularity = max(2, granularity - 1)
        else:
            if granularity >= len(removed):
                # We are already testing individual spans.
                break

            granularity = min(
                len(removed),
                granularity * 2,
            )

    # ------------------------------------------------------------------
    # Final individual verification.
    #
    # This makes the 1-minimal property explicit even if the grouping
    # strategy happened to leave some restorable individual spans.
    # ------------------------------------------------------------------

    print()
    print("-" * 72)
    print("FINAL INDIVIDUAL MINIMALITY PASS")
    print("-" * 72)

    for index in sorted(list(removed)):
        if max_tests is not None and tests >= max_tests:
            print("Reached --max-tests during final pass.")
            break

        candidate_removed = removed - {index}

        candidate_data = remove_kobo_spans(
            baseline_data,
            spans,
            candidate_removed,
        )

        test_directory = output_dir / (
            f"_final_test_{tests:05d}"
        )

        if test_directory.exists():
            shutil.rmtree(test_directory)

        shutil.copytree(
            normalized_directory,
            test_directory,
        )

        (
            test_directory / target_relative_path
        ).write_bytes(candidate_data)

        test_epub = output_dir / (
            f"_final_test_{tests:05d}.epub"
        )

        span = spans[index]

        result = pack_and_benchmark(
            test_directory,
            test_epub,
            reader,
            timeout,
            interval,
            idle_threshold,
            idle_samples,
            f"restore span [{index}] {span.span_id}",
        )

        tests += 1

        if result.good:
            # This span was not actually necessary.
            removed.remove(index)
            print(
                f"    NOT REQUIRED: span [{index}] {span.span_id}"
            )
        else:
            print(
                f"    REQUIRED: span [{index}] {span.span_id}"
            )

        if not keep_tests:
            shutil.rmtree(
                test_directory,
                ignore_errors=True,
            )
            test_epub.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Build final directory.
    # ------------------------------------------------------------------

    final_directory = output_dir / "FINAL"

    if final_directory.exists():
        shutil.rmtree(final_directory)

    shutil.copytree(normalized_directory, final_directory)

    final_data = remove_kobo_spans(
        baseline_data,
        spans,
        removed,
    )

    (
        final_directory / target_relative_path
    ).write_bytes(final_data)

    final_epub = output_dir / "FINAL-minimized.epub"

    print()
    print("Testing FINAL minimized EPUB...")

    final_result = pack_and_benchmark(
        final_directory,
        final_epub,
        reader,
        timeout,
        interval,
        idle_threshold,
        idle_samples,
        "FINAL minimized EPUB",
    )

    tests += 1

    # ------------------------------------------------------------------
    # Reports.
    # ------------------------------------------------------------------

    report_path = output_dir / "KOBO-REMOVALS.txt"

    with report_path.open("w", encoding="utf-8") as f:
        f.write("Kobo span minimization report\n")
        f.write("=" * 72 + "\n\n")
        f.write(f"Target: {target_relative_path}\n")
        f.write(f"Total Kobo spans: {len(spans)}\n")
        f.write(f"Required removals: {len(removed)}\n")
        f.write(f"Reader tests: {tests}\n\n")

        f.write("Removed Kobo spans:\n")
        f.write("-" * 72 + "\n")

        for index in sorted(removed):
            span = spans[index]

            f.write(
                f"[{index:4d}] "
                f"{span.span_id} "
                f"element={span.element_start}-{span.element_end} "
                f"start-tag={span.start_tag_start}-{span.start_tag_end} "
                f"end-tag={span.end_tag_start}-{span.end_tag_end}\n"
            )

        f.write("\n\nAll Kobo spans:\n")
        f.write("-" * 72 + "\n")

        for span in spans:
            status = "REMOVE" if span.index in removed else "KEEP"

            f.write(
                f"[{span.index:4d}] {status:6s} "
                f"{span.span_id} "
                f"element={span.element_start}-{span.element_end}\n"
            )

    # Exact target-file diff between normalized and final versions.
    diff_path = output_dir / "KOBO-DIFF.patch"

    final_target_data = (
        final_directory / target_relative_path
    ).read_bytes()

    diff_lines = difflib.diff_bytes(
        difflib.unified_diff,
        baseline_data.splitlines(keepends=True),
        final_target_data.splitlines(keepends=True),
        fromfile=b"normalized/" + str(target_relative_path).encode(),
        tofile=b"final/" + str(target_relative_path).encode(),
        n=3,
    )

    diff_path.write_bytes(b"".join(diff_lines))

    print()
    print("=" * 72)
    print("MINIMIZATION COMPLETE")
    print("=" * 72)
    print(f"Target:             {target_relative_path}")
    print(f"Kobo spans found:   {len(spans)}")
    print(f"Kobo spans removed: {len(removed)}")
    print(f"Reader tests:       {tests}")
    print()
    print(f"Normalized EPUB:    {normalized_epub}")
    print(f"Final EPUB:         {final_epub}")
    print(f"Removal report:     {report_path}")
    print(f"Exact target diff:  {diff_path}")

    if final_result.good:
        print()
        print("FINAL RESULT: GOOD")
    else:
        print()
        print("WARNING: final benchmark was BAD")

    return {
        "spans": spans,
        "removed": removed,
        "final_directory": final_directory,
        "final_epub": final_epub,
        "tests": tests,
    }


# ---------------------------------------------------------------------------
# Automatic problematic-file isolation
# ---------------------------------------------------------------------------

def find_html_files(directory: Path):
    return [
        p.relative_to(directory)
        for p in sorted(directory.rglob("*"))
        if p.is_file()
        and p.suffix.lower() in XHTML_EXTENSIONS
    ]


def find_bad_html_file(
    normalized_directory: Path,
    output_dir: Path,
    reader: str,
    timeout: float,
    interval: float,
    idle_threshold: float,
    idle_samples: int,
    keep_tests: bool,
    max_tests: int | None,
):
    """
    Test each XHTML file while blanking all other XHTML files.

    The candidate itself is left intact.
    """
    html_files = find_html_files(normalized_directory)

    print()
    print("=" * 72)
    print("AUTOMATIC XHTML FILE ISOLATION")
    print("=" * 72)
    print(f"Candidate HTML/XHTML files: {len(html_files)}")

    tests = 0

    for relative_path in html_files:
        if max_tests is not None and tests >= max_tests:
            print(f"Reached --max-tests={max_tests}.")
            break

        print()
        print(f"[FILE TEST] {relative_path}")

        test_directory = output_dir / (
            f"_file_test_{tests:05d}"
        )

        if test_directory.exists():
            shutil.rmtree(test_directory)

        isolate_html_files(
            normalized_directory,
            relative_path,
            test_directory,
        )

        test_epub = output_dir / (
            f"_file_test_{tests:05d}.epub"
        )

        result = pack_and_benchmark(
            test_directory,
            test_epub,
            reader,
            timeout,
            interval,
            idle_threshold,
            idle_samples,
            str(relative_path),
        )

        tests += 1

        if not keep_tests:
            shutil.rmtree(
                test_directory,
                ignore_errors=True,
            )
            test_epub.unlink(missing_ok=True)

        if not result.good:
            print()
            print(
                f"*** SUFFICIENT: {relative_path}"
            )

            return relative_path, tests

        print(
            f"    insufficient: {relative_path}"
        )

    return None, tests


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

def prepare_input(
    input_path: Path,
    output_dir: Path,
):
    """
    Create a pristine unpacked copy of the input.

    Supports:
        broken.epub
        unpacked_epub_directory/
    """
    source_directory = output_dir / "_source"

    if source_directory.exists():
        shutil.rmtree(source_directory)

    if input_path.is_dir():
        shutil.copytree(input_path, source_directory)
    else:
        unpack_epub(input_path, source_directory)

    return source_directory


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Normalize EPUB XHTML namespaces and minimize Kobo spans "
            "using Tree-sitter byte offsets."
        )
    )

    parser.add_argument(
        "input",
        type=Path,
        help="Broken EPUB file or unpacked EPUB directory",
    )

    parser.add_argument(
        "--html-path",
        type=Path,
        default=None,
        help=(
            "Specific XHTML file to minimize, e.g. "
            "OEBPS/appendix-001.xhtml. "
            "If omitted, automatically isolate the bad XHTML file."
        ),
    )

    parser.add_argument(
        "--epub-reader",
        default="okular",
        help="EPUB reader command (default: okular)",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("epub-minimizer-tests"),
        help="Directory for results and temporary tests",
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"BAD timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"CPU sampling interval (default: {DEFAULT_INTERVAL})",
    )

    parser.add_argument(
        "--idle-threshold",
        type=float,
        default=DEFAULT_IDLE_THRESHOLD,
        help=(
            "CPU percentage considered idle "
            f"(default: {DEFAULT_IDLE_THRESHOLD})"
        ),
    )

    parser.add_argument(
        "--idle-samples",
        type=int,
        default=DEFAULT_IDLE_SAMPLES,
        help=(
            "Consecutive idle samples required for GOOD "
            f"(default: {DEFAULT_IDLE_SAMPLES})"
        ),
    )

    parser.add_argument(
        "--max-tests",
        type=int,
        default=None,
        help="Stop after this many reader tests",
    )

    parser.add_argument(
        "--keep-tests",
        action="store_true",
        help="Keep temporary EPUBs/directories",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    input_path = args.input.resolve()
    output_dir = args.output_dir.resolve()

    if not input_path.exists():
        print(
            f"ERROR: input does not exist: {input_path}",
            file=sys.stderr,
        )
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("EPUB CLEANER / KOBO SPAN MINIMIZER")
    print("=" * 72)
    print(f"Input:          {input_path}")
    print(f"Output:         {output_dir}")
    print(f"Reader:         {args.epub_reader}")
    print(f"Timeout:        {args.timeout}s")
    print(f"CPU interval:   {args.interval}s")
    print(f"Idle threshold: {args.idle_threshold}%")
    print(f"Idle samples:   {args.idle_samples}")

    # ------------------------------------------------------------------
    # Prepare pristine source.
    # ------------------------------------------------------------------

    source_directory = prepare_input(
        input_path,
        output_dir,
    )

    # ------------------------------------------------------------------
    # Stage 1:
    # namespace normalization.
    # ------------------------------------------------------------------

    normalize_directory(source_directory)

    # Keep an immutable-ish normalized baseline for all tests.
    normalized_directory = output_dir / "_normalized"

    if normalized_directory.exists():
        shutil.rmtree(normalized_directory)

    shutil.copytree(
        source_directory,
        normalized_directory,
    )

    # ------------------------------------------------------------------
    # Determine target XHTML file.
    # ------------------------------------------------------------------

    if args.html_path is not None:
        target_relative_path = args.html_path

        target = normalized_directory / target_relative_path

        if not target.exists():
            print(
                f"ERROR: XHTML file does not exist: {target}",
                file=sys.stderr,
            )
            return 2

        print()
        print(
            f"Using explicitly specified target: "
            f"{target_relative_path}"
        )

    else:
        target_relative_path, file_tests = find_bad_html_file(
            normalized_directory,
            output_dir,
            args.epub_reader,
            args.timeout,
            args.interval,
            args.idle_threshold,
            args.idle_samples,
            args.keep_tests,
            args.max_tests,
        )

        if target_relative_path is None:
            print()
            print(
                "ERROR: No individual XHTML file was sufficient "
                "to reproduce the BAD behavior."
            )
            return 1

    # ------------------------------------------------------------------
    # Stage 2:
    # Kobo span minimization.
    # ------------------------------------------------------------------

    result = minimize_kobo_spans(
        normalized_directory=normalized_directory,
        target_relative_path=target_relative_path,
        output_dir=output_dir,
        reader=args.epub_reader,
        timeout=args.timeout,
        interval=args.interval,
        idle_threshold=args.idle_threshold,
        idle_samples=args.idle_samples,
        max_tests=args.max_tests,
        keep_tests=args.keep_tests,
    )

    if result is None:
        return 1

    print()
    print("=" * 72)
    print("DONE")
    print("=" * 72)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
