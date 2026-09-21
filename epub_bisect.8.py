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

# DEFAULT_TIMEOUT = 5.0
DEFAULT_TIMEOUT = 3.0
# DEFAULT_INTERVAL = 0.05
DEFAULT_INTERVAL = 0.1
DEFAULT_IDLE_THRESHOLD = 5.0
# DEFAULT_IDLE_SAMPLES = 3
DEFAULT_IDLE_SAMPLES = 10


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


@dataclass(frozen=True)
class XhtmlElement:
    index: int
    tag_name: str
    element_start: int
    element_end: int
    start_tag_start: int
    start_tag_end: int
    end_tag_start: int
    end_tag_end: int
    id_value: str | None
    class_value: str | None


GENERIC_EXCLUDED_TAGS = {
    b"html",
    b"head",
    b"body",
    b"script",
    b"style",
}


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
    print(f"writing epub {output_epub!r}")
    output_epub.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(
            output_epub,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as z:
        mimetype = directory / "mimetype"

        # print(f"epub: adding file: mimetype") # debug
        # EPUB requires mimetype to be first and uncompressed.
        if mimetype.exists():
            z.write(mimetype, "mimetype", compress_type=zipfile.ZIP_STORED)

        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue

            rel = path.relative_to(directory)

            if rel.as_posix() == "mimetype":
                continue

            # dont recurse
            # if rel.as_posix() == "candidate.epub":
            if rel.suffix == ".epub":
                continue

            # remove unnecessary large files: images, fonts, ...
            if rel.suffix in (".jpg", ".otf"):
                continue

            # print(f"epub: adding file: {rel!r}") # debug
            # z.write(path, rel.as_posix(), compress_type=zipfile.ZIP_DEFLATED)
            # store uncompressed files = faster write, faster read
            z.write(path, rel.as_posix(), compress_type=zipfile.ZIP_STORED)

    print(f"writing epub done")


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
                print("proc is not running -> good")
                good = True
                break

            cpu = proc.cpu_percent(None)
            peak_cpu = max(peak_cpu, cpu)

            if verbose:
                print(f"    CPU: {cpu:6.1f}%")

            if cpu <= idle_threshold:
                idle_count += 1

                if idle_count >= idle_samples:
                    print("idle_count >= idle_samples -> good")
                    good = True
                    break
            else:
                idle_count = 0

        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            print(f"exception: {type(exc).__name__}: {exc} -> good")
            good = True
            break

    print(f"good={good}")

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

    # not reached?
    raise 5

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


# generic XHTML functions

def find_xhtml_elements(data: bytes) -> list[XhtmlElement]:
    """
    Find XHTML elements whose opening AND closing tags can be removed.

    Only the wrapper tags are candidates. The element contents are preserved.

    Structural/container elements that would make the whole document
    semantically meaningless are excluded initially.
    """
    result = []

    for element in find_elements(data):
        start_tag, end_tag = get_element_start_and_end_tags(
            data,
            element,
        )

        if start_tag is None or end_tag is None:
            continue

        tag_name = get_tag_name(data, start_tag)

        if tag_name is None:
            continue

        if tag_name in GENERIC_EXCLUDED_TAGS:
            continue

        id_value = get_attribute_value(
            data,
            start_tag,
            b"id",
        )

        class_value = get_attribute_value(
            data,
            start_tag,
            b"class",
        )

        id_text = (
            id_value.decode("utf-8", errors="replace")
            if id_value is not None
            else None
        )

        class_text = (
            class_value.decode("utf-8", errors="replace")
            if class_value is not None
            else None
        )

        result.append(
            XhtmlElement(
                index=len(result),
                tag_name=tag_name.decode(
                    "utf-8",
                    errors="replace",
                ),
                element_start=element.start_byte,
                element_end=element.end_byte,
                start_tag_start=start_tag.start_byte,
                start_tag_end=start_tag.end_byte,
                end_tag_start=end_tag.start_byte,
                end_tag_end=end_tag.end_byte,
                id_value=id_text,
                class_value=class_text,
            )
        )

    return result


def removal_ranges_for_elements(
        elements: list[XhtmlElement],
        indices: set[int],
    ):
    ranges = []

    for i in indices:
        element = elements[i]

        ranges.append(
            (
                element.start_tag_start,
                element.start_tag_end,
            )
        )

        ranges.append(
            (
                element.end_tag_start,
                element.end_tag_end,
            )
        )

    return merge_ranges(ranges)


def remove_xhtml_element_wrappers(
        data: bytes,
        elements: list[XhtmlElement],
        indices: set[int],
    ) -> bytes:
    return apply_deletions(
        data,
        removal_ranges_for_elements(
            elements,
            indices,
        ),
    )


def minimize_xhtml_elements(
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
    Find a 1-minimal set of XHTML element wrappers whose removal makes the
    EPUB GOOD.

    Only opening/closing tags are removed. Element contents remain intact.

    This is 1-minimal, not globally minimum.
    """

    target_path = (
        normalized_directory /
        target_relative_path
    )

    if not target_path.exists():
        raise FileNotFoundError(target_path)

    baseline_data = target_path.read_bytes()

    elements = find_xhtml_elements(baseline_data)

    print()
    print("=" * 72)
    print("STAGE 2: GENERIC XHTML ELEMENT MINIMIZATION")
    print("=" * 72)
    print(f"Target: {target_relative_path}")
    print(f"Candidate elements: {len(elements)}")

    if not elements:
        print("ERROR: no candidate XHTML elements found.")
        return None

    for element in elements:
        description = f"<{element.tag_name}>"

        if element.id_value:
            description += f' id="{element.id_value}"'

        if element.class_value:
            description += f' class="{element.class_value}"'

        print(
            f"    [{element.index:4d}] "
            f"{description} "
            f"bytes {element.element_start}-"
            f"{element.element_end}"
        )

    tests = 0

    # ---------------------------------------------------------------
    # Test normalized baseline.
    # ---------------------------------------------------------------

    normalized_epub = output_dir / "NORMALIZED.epub"

    pack_epub(
        normalized_directory,
        normalized_epub,
    )

    print()
    print("Testing normalized baseline...")

    baseline_result = benchmark_epub(
        normalized_epub,
        reader,
        timeout,
        interval,
        idle_threshold,
        idle_samples,
    )

    tests += 1

    print(
        f"    => {'GOOD' if baseline_result.good else 'BAD'} "
        f"(peak {baseline_result.peak_cpu:.1f}%, "
        f"{baseline_result.elapsed:.2f}s)"
    )

    if baseline_result.good:
        print()
        print("Namespace normalization already fixed the EPUB.")
        return {
            "elements": elements,
            "removed": set(),
            "tests": tests,
        }

    # ---------------------------------------------------------------
    # Remove ALL candidate element wrappers.
    # ---------------------------------------------------------------

    all_indices = set(range(len(elements)))

    all_removed_data = remove_xhtml_element_wrappers(
        baseline_data,
        elements,
        all_indices,
    )

    all_removed_directory = (
        output_dir /
        "_all_elements_removed"
    )

    if all_removed_directory.exists():
        shutil.rmtree(all_removed_directory)

    shutil.copytree(
        normalized_directory,
        all_removed_directory,
    )

    (
        all_removed_directory /
        target_relative_path
    ).write_bytes(all_removed_data)

    all_removed_epub = (
        output_dir /
        "_all_elements_removed.epub"
    )

    print()
    print(
        "Testing EPUB with ALL candidate XHTML "
        "element wrappers removed..."
    )

    result = pack_and_benchmark(
        all_removed_directory,
        all_removed_epub,
        reader,
        timeout,
        interval,
        idle_threshold,
        idle_samples,
        "all XHTML element wrappers removed",
    )

    tests += 1

    if not result.good:
        print()
        print(
            "ERROR: Removing all candidate XHTML element wrappers "
            "does not make the EPUB GOOD."
        )
        print()
        print(
            "This means the CPU problem is NOT caused solely by "
            "the opening/closing tags of ordinary XHTML elements."
        )
        print()
        print(
            "The next level would be content-level minimization "
            "(entire elements, text nodes, attributes, CSS, etc.)."
        )

        if not keep_tests:
            shutil.rmtree(
                all_removed_directory,
                ignore_errors=True,
            )
            all_removed_epub.unlink(
                missing_ok=True
            )

        return None

    if not keep_tests:
        shutil.rmtree(
            all_removed_directory,
            ignore_errors=True,
        )
        all_removed_epub.unlink(
            missing_ok=True
        )

    # ---------------------------------------------------------------
    # Delta debugging.
    # ---------------------------------------------------------------

    removed = set(all_indices)

    granularity = 2

    print()
    print("-" * 72)
    print("MINIMIZING XHTML ELEMENT WRAPPER REMOVALS")
    print("-" * 72)

    while removed:
        if (
            max_tests is not None
            and tests >= max_tests
        ):
            print(
                f"Reached --max-tests={max_tests}."
            )
            break

        ordered_removed = sorted(removed)

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
            f"removed={len(removed)})"
        )

        for group_number, group in enumerate(
            groups,
            1,
        ):
            if (
                max_tests is not None
                and tests >= max_tests
            ):
                break

            group_set = set(group)

            # Restore this group.
            candidate_removed = (
                removed -
                group_set
            )

            candidate_data = (
                remove_xhtml_element_wrappers(
                    baseline_data,
                    elements,
                    candidate_removed,
                )
            )

            test_directory = (
                output_dir /
                f"_test_{tests:05d}"
            )

            if test_directory.exists():
                shutil.rmtree(test_directory)

            shutil.copytree(
                normalized_directory,
                test_directory,
            )

            (
                test_directory /
                target_relative_path
            ).write_bytes(candidate_data)

            test_epub = (
                output_dir /
                f"_test_{tests:05d}.epub"
            )

            description = (
                f"restore group "
                f"{group_number}/{len(groups)} "
                f"({len(group)} elements)"
            )

            result = pack_and_benchmark(
                test_directory,
                test_epub,
                reader,
                timeout,
                interval,
                idle_threshold,
                idle_samples,
                description,
            )

            tests += 1

            if result.good:
                removed = candidate_removed
                progress = True

                print(
                    f"    RESTORED {len(group)} elements; "
                    f"{len(removed)} remain removed"
                )

            else:
                print(
                    f"    KEEP REMOVING this group; "
                    f"{len(group)} elements implicated"
                )

            if not keep_tests:
                shutil.rmtree(
                    test_directory,
                    ignore_errors=True,
                )
                test_epub.unlink(
                    missing_ok=True
                )

        if not removed:
            break

        if progress:
            granularity = max(
                2,
                granularity - 1,
            )
        else:
            if granularity >= len(removed):
                break

            granularity = min(
                len(removed),
                granularity * 2,
            )

    # ---------------------------------------------------------------
    # Explicit 1-minimality pass.
    # ---------------------------------------------------------------

    print()
    print("-" * 72)
    print("FINAL INDIVIDUAL MINIMALITY PASS")
    print("-" * 72)

    for index in sorted(list(removed)):

        if (
            max_tests is not None
            and tests >= max_tests
        ):
            print(
                "Reached --max-tests during final pass."
            )
            break

        candidate_removed = (
            removed -
            {index}
        )

        candidate_data = (
            remove_xhtml_element_wrappers(
                baseline_data,
                elements,
                candidate_removed,
            )
        )

        test_directory = (
            output_dir /
            f"_final_test_{tests:05d}"
        )

        if test_directory.exists():
            shutil.rmtree(test_directory)

        shutil.copytree(
            normalized_directory,
            test_directory,
        )

        (
            test_directory /
            target_relative_path
        ).write_bytes(candidate_data)

        test_epub = (
            output_dir /
            f"_final_test_{tests:05d}.epub"
        )

        element = elements[index]

        description = (
            f"restore [{index}] "
            f"<{element.tag_name}>"
        )

        if element.id_value:
            description += (
                f' id="{element.id_value}"'
            )

        result = pack_and_benchmark(
            test_directory,
            test_epub,
            reader,
            timeout,
            interval,
            idle_threshold,
            idle_samples,
            description,
        )

        tests += 1

        if result.good:
            removed.remove(index)

            print(
                f"    NOT REQUIRED: "
                f"[{index}] "
                f"<{element.tag_name}>"
            )
        else:
            print(
                f"    REQUIRED: "
                f"[{index}] "
                f"<{element.tag_name}>"
            )

        if not keep_tests:
            shutil.rmtree(
                test_directory,
                ignore_errors=True,
            )
            test_epub.unlink(
                missing_ok=True
            )

    # ---------------------------------------------------------------
    # Construct final EPUB.
    # ---------------------------------------------------------------

    final_directory = (
        output_dir /
        "FINAL"
    )

    if final_directory.exists():
        shutil.rmtree(final_directory)

    shutil.copytree(
        normalized_directory,
        final_directory,
    )

    final_data = (
        remove_xhtml_element_wrappers(
            baseline_data,
            elements,
            removed,
        )
    )

    (
        final_directory /
        target_relative_path
    ).write_bytes(final_data)

    final_epub = (
        output_dir /
        "FINAL-minimized.epub"
    )

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

    # ---------------------------------------------------------------
    # Report.
    # ---------------------------------------------------------------

    report_path = (
        output_dir /
        "XHTML-ELEMENT-REMOVALS.txt"
    )

    with report_path.open(
        "w",
        encoding="utf-8",
    ) as f:

        f.write(
            "Generic XHTML element minimization report\n"
        )
        f.write("=" * 72 + "\n\n")

        f.write(
            f"Target: {target_relative_path}\n"
        )

        f.write(
            f"Candidate elements: {len(elements)}\n"
        )

        f.write(
            f"Required wrapper removals: {len(removed)}\n"
        )

        f.write(
            f"Reader tests: {tests}\n\n"
        )

        f.write(
            "REQUIRED REMOVALS\n"
        )
        f.write(
            "-" * 72 + "\n"
        )

        for index in sorted(removed):
            element = elements[index]

            f.write(
                f"[{index:4d}] "
                f"<{element.tag_name}>"
            )

            if element.id_value:
                f.write(
                    f' id="{element.id_value}"'
                )

            if element.class_value:
                f.write(
                    f' class="{element.class_value}"'
                )

            f.write(
                f" element={element.element_start}-"
                f"{element.element_end}"
            )

            f.write(
                f" start-tag={element.start_tag_start}-"
                f"{element.start_tag_end}"
            )

            f.write(
                f" end-tag={element.end_tag_start}-"
                f"{element.end_tag_end}\n"
            )

    # ---------------------------------------------------------------
    # Exact byte-level diff.
    # ---------------------------------------------------------------

    diff_path = (
        output_dir /
        "XHTML-ELEMENT-DIFF.patch"
    )

    final_target_data = (
        final_directory /
        target_relative_path
    ).read_bytes()

    diff_lines = difflib.diff_bytes(
        difflib.unified_diff,
        baseline_data.splitlines(
            keepends=True
        ),
        final_target_data.splitlines(
            keepends=True
        ),
        fromfile=(
            b"normalized/" +
            str(target_relative_path).encode()
        ),
        tofile=(
            b"final/" +
            str(target_relative_path).encode()
        ),
        n=3,
    )

    diff_path.write_bytes(
        b"".join(diff_lines)
    )

    print()
    print("=" * 72)
    print("GENERIC XHTML MINIMIZATION COMPLETE")
    print("=" * 72)
    print(
        f"Target:               {target_relative_path}"
    )
    print(
        f"Candidate elements:   {len(elements)}"
    )
    print(
        f"Elements removed:     {len(removed)}"
    )
    print(
        f"Reader tests:         {tests}"
    )
    print()
    print(
        f"Final EPUB:           {final_epub}"
    )
    print(
        f"Removal report:       {report_path}"
    )
    print(
        f"Exact target diff:    {diff_path}"
    )

    print()
    print(
        "FINAL RESULT: "
        + ("GOOD" if final_result.good else "BAD")
    )

    return {
        "elements": elements,
        "removed": removed,
        "final_directory": final_directory,
        "final_epub": final_epub,
        "tests": tests,
    }


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
        print(f"Total <span> elements: {sum(1 for n in walk(parse_tree(data).root_node) if n.type == 'element' and get_tag_name(data, n).lower() == b'span')}")
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







# TODO remove
r'''

# FIXME remove replaced functions above ^^^^^^^^^^^

# ---------------------------------------------------------------------------
# Randomized GOOD-case XHTML minimizer
# ---------------------------------------------------------------------------

import json
import random
from dataclasses import dataclass

@dataclass(frozen=True)
class XhtmlElement:
    index: int
    tag_name: bytes
    element_start: int
    element_end: int
    start_tag_start: int
    start_tag_end: int
    end_tag_start: int
    end_tag_end: int
    id_value: str | None
    class_value: str | None


def find_xhtml_elements(data: bytes) -> list[XhtmlElement]:
    """
    Find HTML/XHTML elements that have both an opening and closing tag.

    Removing an element means removing ONLY its opening and closing tags.
    Its contents are preserved byte-for-byte.
    """
    # tree = PARSER.parse(data)
    tree = parse_tree(data)

    root = tree.root_node

    result = []

    for node in walk(root):
        if node.type != "element":
            continue

        start_tag = direct_child(node, "start_tag")
        end_tag = direct_child(node, "end_tag")

        if start_tag is None or end_tag is None:
            continue

        tag_name = get_tag_name(data, start_tag)

        # Don't touch the structural elements.
        if tag_name.lower() in {
            b"html",
            b"head",
            b"body",
            b"script",
            b"style",
        }:
            continue

        attrs = list(get_attribute_nodes(start_tag))

        id_value = None
        class_value = None

        for attr in attrs:
            name, value = parse_attribute_name_and_value(data, attr)

            if name is None:
                continue

            if name.lower() == b"id" and value is not None:
                id_value = value.decode("utf-8", errors="replace")

            elif name.lower() == b"class" and value is not None:
                class_value = value.decode("utf-8", errors="replace")

        result.append(
            XhtmlElement(
                index=len(result),
                tag_name=tag_name,
                element_start=node.start_byte,
                element_end=node.end_byte,
                start_tag_start=start_tag.start_byte,
                start_tag_end=start_tag.end_byte,
                end_tag_start=end_tag.start_byte,
                end_tag_end=end_tag.end_byte,
                id_value=id_value,
                class_value=class_value,
            )
        )

    return result


def removal_ranges_for_elements(
        elements: list[XhtmlElement],
        removed: set[int],
    ) -> list[tuple[int, int]]:
    """
    Return byte ranges corresponding ONLY to the opening and closing tags
    of the selected elements.
    """
    ranges = []

    for i in removed:
        e = elements[i]

        ranges.append((e.start_tag_start, e.start_tag_end))
        ranges.append((e.end_tag_start, e.end_tag_end))

    return ranges


def remove_xhtml_elements(
        data: bytes,
        elements: list[XhtmlElement],
        removed: set[int],
    ) -> bytes:
    ranges = removal_ranges_for_elements(elements, removed)
    return apply_deletions(data, ranges)


def describe_element(e: XhtmlElement) -> str:
    parts = [e.tag_name.decode("utf-8", errors="replace")]

    if e.id_value:
        parts.append(f"id={e.id_value!r}")

    if e.class_value:
        parts.append(f"class={e.class_value!r}")

    return "<" + " ".join(parts) + ">"


# ---------------------------------------------------------------------------
# Randomized search
# ---------------------------------------------------------------------------

class RandomGoodSearch:
    def __init__(
        self,
        *,
        elements,
        test_removed,
        seed=None,
        checkpoint_path=None,
        verbose=True,
    ):
        self.elements = elements
        self.test_removed = test_removed
        self.rng = random.Random(seed)
        self.checkpoint_path = checkpoint_path
        self.verbose = verbose

        # Cache:
        #   frozenset(removed element indices) -> GOOD/BAD
        self.cache = {}

        self.tests = 0
        self.good_tests = 0
        self.bad_tests = 0

    def log(self, text):
        if self.verbose:
            print(text, flush=True)

    def test(self, removed: set[int]) -> bool:
        key = frozenset(removed)

        if key in self.cache:
            result = self.cache[key]

            self.log(
                f"[CACHE] {'GOOD' if result else 'BAD'} "
                f"removed={len(removed)}"
            )

            return result

        self.tests += 1

        self.log(
            f"[TEST {self.tests}] "
            f"removed={len(removed)} / {len(self.elements)}"
        )

        result = self.test_removed(removed)

        self.cache[key] = result

        if result:
            self.good_tests += 1
            self.log(
                f"[GOOD] removed={len(removed)} "
                f"(good={self.good_tests}, bad={self.bad_tests})"
            )
        else:
            self.bad_tests += 1
            self.log(
                f"[BAD] removed={len(removed)} "
                f"(good={self.good_tests}, bad={self.bad_tests})"
            )

        return result

    def save_checkpoint(self, removed: set[int], phase: str):
        if not self.checkpoint_path:
            return

        state = {
            "phase": phase,
            "removed": sorted(removed),
            "tests": self.tests,
            "good_tests": self.good_tests,
            "bad_tests": self.bad_tests,
            "cache": [
                {
                    "removed": sorted(key),
                    "good": value,
                }
                for key, value in self.cache.items()
            ],
        }

        tmp = self.checkpoint_path + ".tmp"

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)

        os.replace(tmp, self.checkpoint_path)

    def load_checkpoint(self):
        if not self.checkpoint_path:
            return None

        if not os.path.exists(self.checkpoint_path):
            return None

        with open(self.checkpoint_path, "r", encoding="utf-8") as f:
            state = json.load(f)

        self.tests = state.get("tests", 0)
        self.good_tests = state.get("good_tests", 0)
        self.bad_tests = state.get("bad_tests", 0)

        for item in state.get("cache", []):
            self.cache[frozenset(item["removed"])] = bool(item["good"])

        return set(state["removed"]), state.get("phase", "unknown")

    # ------------------------------------------------------------------
    # Phase 1:
    # BAD -> GOOD
    #
    # Randomly remove increasingly large groups.
    # ------------------------------------------------------------------

    def find_initial_good(self, removed: set[int]) -> set[int]:
        remaining = set(range(len(self.elements))) - removed

        if self.test(removed):
            return removed

        # Start with a fairly large fraction.
        batch_fraction = 0.50

        while remaining:
            batch_size = max(
                1,
                int(len(remaining) * batch_fraction),
            )

            batch = set(
                self.rng.sample(
                    list(remaining),
                    min(batch_size, len(remaining)),
                )
            )

            candidate = removed | batch

            self.log(
                f"[REMOVE-BATCH] trying {len(batch)} elements; "
                f"total removed would be {len(candidate)}"
            )

            if self.test(candidate):
                # SUCCESS: commit all of them.
                removed = candidate
                remaining -= batch

                self.log(
                    f"[KEEP-BATCH] removed={len(removed)}, "
                    f"remaining={len(remaining)}"
                )

                # Once we're GOOD, we're done with this phase.
                return removed

            # Still BAD.
            #
            # Increase the removal fraction. We need to become more
            # aggressive, not less.
            batch_fraction = min(
                0.90,
                batch_fraction * 1.5,
            )

            # Randomize the remaining elements again.
            remaining_list = list(remaining)
            self.rng.shuffle(remaining_list)

            self.log(
                f"[STILL-BAD] increasing removal batch fraction "
                f"to {batch_fraction:.2f}"
            )

            # If the fraction has become large enough, eventually
            # we'll approach removing everything.
            if batch_fraction >= 0.90:
                candidate = set(range(len(self.elements)))

                self.log(
                    "[REMOVE-ALL] testing all element wrappers removed"
                )

                if self.test(candidate):
                    return candidate

                raise RuntimeError(
                    "Removing all candidate XHTML element wrappers "
                    "does not make the EPUB GOOD."
                )

        raise RuntimeError(
            "Could not find a GOOD EPUB."
        )

    # ------------------------------------------------------------------
    # Phase 2:
    # GOOD -> restore as much as possible
    #
    # This is deliberately randomized and conservative.
    # ------------------------------------------------------------------

    def restore_random_batches(self, removed: set[int]) -> set[int]:
        restored = set(range(len(self.elements))) - removed

        # Elements currently removed and therefore candidates for restore.
        candidates = list(removed)

        self.rng.shuffle(candidates)

        if not candidates:
            return removed

        # Start by trying fairly small restoration batches.
        # This strongly biases us toward GOOD tests.
        fraction = 0.02

        no_progress_rounds = 0
        round_number = 0

        while candidates:
            round_number += 1

            self.rng.shuffle(candidates)

            batch_size = max(
                1,
                int(len(candidates) * fraction),
            )

            batch = set(candidates[:batch_size])

            candidate_removed = removed - batch

            self.log(
                f"\n[RESTORE ROUND {round_number}] "
                f"trying to restore {len(batch)} elements; "
                f"removed would become {len(candidate_removed)}"
            )

            if self.test(candidate_removed):
                # GOOD: keep the restorations.
                removed = candidate_removed

                candidates = [
                    i for i in candidates
                    if i not in batch
                ]

                no_progress_rounds = 0

                self.log(
                    f"[KEEP-RESTORE] restored={len(batch)}, "
                    f"removed={len(removed)}"
                )

                # Since this worked, cautiously increase batch size.
                fraction = min(
                    0.25,
                    fraction * 1.5,
                )

                self.save_checkpoint(
                    removed,
                    "restore-random",
                )

            else:
                # BAD: undo this attempted restoration.
                #
                # Do NOT branch into subgroups here. That's the behavior
                # that caused the enormous runtime of the old algorithm.
                no_progress_rounds += 1

                self.log(
                    f"[REJECT-RESTORE] batch of {len(batch)} "
                    f"was BAD"
                )

                # Make the next attempt smaller.
                fraction *= 0.5

                if fraction < 1.0 / max(len(candidates), 1):
                    fraction = 1.0 / max(len(candidates), 1)

                # If we're down to one element, test it once.
                if len(candidates) == 1:
                    break

                if no_progress_rounds >= 10:
                    self.log(
                        "[RESTORE] 10 consecutive rejected batches; "
                        "switching to single-element pass"
                    )
                    break

        return removed

    # ------------------------------------------------------------------
    # Phase 3:
    # Make the result 1-minimal.
    #
    # For every element still removed, see whether restoring just that
    # element keeps the EPUB GOOD.
    # ------------------------------------------------------------------

    def final_single_element_pass(
        self,
        removed: set[int],
    ) -> set[int]:

        candidates = list(removed)
        self.rng.shuffle(candidates)

        self.log(
            f"\n[FINAL PASS] testing {len(candidates)} "
            f"removed elements individually"
        )

        for n, i in enumerate(candidates, 1):
            if i not in removed:
                continue

            candidate_removed = removed - {i}

            self.log(
                f"[FINAL {n}/{len(candidates)}] "
                f"trying to restore element {i}"
            )

            if self.test(candidate_removed):
                # It was unnecessary.
                removed = candidate_removed

                self.log(
                    f"[RESTORE] element {i} is NOT necessary"
                )
            else:
                self.log(
                    f"[KEEP-REMOVED] element {i} is necessary"
                )

            self.save_checkpoint(
                removed,
                "final-single",
            )

        return removed


# ---------------------------------------------------------------------------
# Main minimizer
# ---------------------------------------------------------------------------

def minimize_xhtml_elements_random(
        *,
        normalized_epub: Path,
        html_path: Path,
        output_epub: Path,
        reader: str,
        timeout: float,
        interval: float,
        idle_threshold: float,
        idle_samples: int,
        seed: int | None,
        checkpoint_path: Path | None,
    ):
    """
    Randomized BAD -> GOOD -> 1-minimal XHTML element minimizer.
    """

    print()
    print("============================================================")
    print(" RANDOMIZED XHTML MINIMIZER")
    print("============================================================")
    print()

    data = html_path.read_bytes()

    elements = find_xhtml_elements(data)

    print(f"[INFO] XHTML file: {html_path}")
    print(f"[INFO] Size: {len(data):,} bytes")
    print(f"[INFO] Candidate elements: {len(elements):,}")

    if seed is None:
        seed = random.randrange(0, 2**63)

    print(f"[INFO] Random seed: {seed}")

    if checkpoint_path:
        print(f"[INFO] Checkpoint: {checkpoint_path}")

    # ---------------------------------------------------------------
    # This callback creates an EPUB for each candidate and benchmarks
    # it.
    # ---------------------------------------------------------------

    def test_removed(removed: set[int]) -> bool:
        candidate_data = remove_xhtml_elements(
            data,
            elements,
            removed,
        )

        # Create a temporary EPUB from the normalized EPUB, replacing
        # only the candidate XHTML file.
        with tempfile.TemporaryDirectory(
            prefix="epub-random-min-"
        ) as tmp:

            tmpdir = Path(tmp)

            unpack_epub(
                normalized_epub,
                tmpdir,
            )

            candidate_file = tmpdir / html_path.name

            # If html_path is relative to the EPUB extraction directory,
            # this handles paths such as OEBPS/Text/foo.xhtml.
            #
            # The caller should pass html_path relative to the EPUB root.
            candidate_file = tmpdir / html_path

            candidate_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            candidate_file.write_bytes(candidate_data)

            candidate_epub = tmpdir / "candidate.epub"

            pack_epub(
                tmpdir,
                candidate_epub,
            )

            return benchmark_epub(
                candidate_epub,
                reader=reader,
                timeout=timeout,
                interval=interval,
                idle_threshold=idle_threshold,
                idle_samples=idle_samples,
            )

    search = RandomGoodSearch(
        elements=elements,
        test_removed=test_removed,
        seed=seed,
        checkpoint_path=(
            str(checkpoint_path)
            if checkpoint_path
            else None
        ),
    )

    # ---------------------------------------------------------------
    # Resume if checkpoint exists.
    # ---------------------------------------------------------------

    checkpoint = search.load_checkpoint()

    if checkpoint:
        removed, phase = checkpoint

        print(
            f"[RESUME] checkpoint phase={phase}, "
            f"removed={len(removed)}"
        )

        # If checkpoint was saved during the restore/final phases,
        # continue from there.
        if phase == "restore-random":
            removed = search.restore_random_batches(removed)

        elif phase == "final-single":
            removed = search.final_single_element_pass(removed)

        else:
            # Unknown/intermediate checkpoint: verify it.
            if not search.test(removed):
                print(
                    "[RESUME] checkpoint is BAD; restarting "
                    "from BAD state"
                )
                removed = set()
                removed = search.find_initial_good(removed)

    else:
        # -----------------------------------------------------------
        # Start from BAD: no elements removed.
        # -----------------------------------------------------------

        removed = set()

        print()
        print("[PHASE 1] BAD -> GOOD")
        print()

        if search.test(removed):
            raise RuntimeError(
                "The normalized EPUB is already GOOD. "
                "There is nothing to minimize."
            )

        removed = search.find_initial_good(removed)

    # ---------------------------------------------------------------
    # GOOD -> restore random groups
    # ---------------------------------------------------------------

    print()
    print("[PHASE 2] GOOD -> restore random groups")
    print()

    removed = search.restore_random_batches(removed)

    # ---------------------------------------------------------------
    # Final 1-minimal pass
    # ---------------------------------------------------------------

    print()
    print("[PHASE 3] final single-element minimality pass")
    print()

    removed = search.final_single_element_pass(removed)

    # ---------------------------------------------------------------
    # Final verification
    # ---------------------------------------------------------------

    print()
    print("[FINAL] verifying result...")

    if not search.test(removed):
        raise RuntimeError(
            "Internal error: final result is BAD."
        )

    print()
    print("============================================================")
    print(" RESULT")
    print("============================================================")
    print()
    print(f"Candidate elements : {len(elements):,}")
    print(f"Elements removed   : {len(removed):,}")
    print(f"Elements remaining : {len(elements) - len(removed):,}")
    print(f"Tests              : {search.tests:,}")
    print(f"GOOD tests         : {search.good_tests:,}")
    print(f"BAD tests          : {search.bad_tests:,}")
    print()

    # ---------------------------------------------------------------
    # Write final XHTML
    # ---------------------------------------------------------------

    final_data = remove_xhtml_elements(
        data,
        elements,
        removed,
    )

    # Write a human-readable removal report.
    report_path = output_epub.with_name(
        output_epub.stem + "-XHTML-REMOVALS.txt"
    )

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(
            "Randomized XHTML minimizer removal report\n"
        )
        f.write(f"Source: {html_path}\n")
        f.write(f"Seed: {seed}\n")
        f.write(f"Candidates: {len(elements)}\n")
        f.write(f"Removed: {len(removed)}\n")
        f.write("\n")

        for i in sorted(removed):
            e = elements[i]

            f.write(
                f"[{i}] "
                f"{describe_element(e)} "
                f"bytes "
                f"{e.element_start}-{e.element_end}\n"
            )

    # ---------------------------------------------------------------
    # Build final EPUB
    # ---------------------------------------------------------------

    with tempfile.TemporaryDirectory(
        prefix="epub-final-"
    ) as tmp:

        tmpdir = Path(tmp)

        unpack_epub(
            normalized_epub,
            tmpdir,
        )

        final_html = tmpdir / html_path

        final_html.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        final_html.write_bytes(final_data)

        pack_epub(
            tmpdir,
            output_epub,
        )

    print(f"[OUTPUT] {output_epub}")
    print(f"[OUTPUT] {report_path}")

    # ---------------------------------------------------------------
    # Optional exact XHTML diff
    # ---------------------------------------------------------------

    diff_path = output_epub.with_name(
        output_epub.stem + "-XHTML-DIFF.patch"
    )

    original_lines = data.decode(
        "utf-8",
        errors="replace",
    ).splitlines(keepends=True)

    final_lines = final_data.decode(
        "utf-8",
        errors="replace",
    ).splitlines(keepends=True)

    diff = difflib.unified_diff(
        original_lines,
        final_lines,
        fromfile=str(html_path),
        tofile=str(html_path) + " (minimized)",
    )

    diff_path.write_text(
        "".join(diff),
        encoding="utf-8",
    )

    print(f"[OUTPUT] {diff_path}")

    return removed
'''



# ============================================================================
# RANDOMIZED XHTML CONTENT / SUBTREE MINIMIZER
#
# Unlike the old wrapper minimizer, this removes COMPLETE XHTML ELEMENTS:
#
#     <div class="foo"> ... entire subtree ... </div>
#
# The bytes are deleted directly. There is NO XML/HTML serialization.
#
# Search strategy:
#
#   BAD
#     |
#     | randomly remove large groups of subtrees
#     v
#   GOOD
#     |
#     | randomly restore groups
#     v
#   GOOD with as much content restored as possible
#     |
#     | single-element final minimality pass
#     v
#   1-minimal GOOD result
#
# ============================================================================

import json
import random
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Candidate representation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class XhtmlSubtree:
    index: int

    tag_name: bytes

    # Complete byte range of the element.
    start_byte: int
    end_byte: int

    # Tree structure among candidates.
    parent: int | None
    depth: int

    id_value: str | None
    class_value: str | None


# ---------------------------------------------------------------------------
# Find complete XHTML elements
# ---------------------------------------------------------------------------

def find_xhtml_subtrees(data: bytes) -> list[XhtmlSubtree]:
    """
    Find complete XHTML element subtrees.

    The byte range is:

        node.start_byte : node.end_byte

    so removing a candidate removes EVERYTHING in that element.

    Structural elements are excluded:
        html
        head
        body
        script
        style

    Elements without a closing tag are excluded for now because this
    minimizer deliberately operates on complete element subtrees.
    """

    # tree = PARSER.parse(data)
    tree = parse_tree(data)

    root = tree.root_node

    raw_nodes = []

    for node in walk(root):
        if node.type != "element":
            continue

        start_tag = direct_child(node, "start_tag")
        end_tag = direct_child(node, "end_tag")

        if start_tag is None or end_tag is None:
            continue

        tag_name = get_tag_name(data, start_tag)

        if tag_name.lower() in {
            b"html",
            b"head",
            b"body",
            b"script",
            b"style",
        }:
            continue

        attrs = list(get_attribute_nodes(start_tag))

        id_value = None
        class_value = None

        for attr in attrs:
            name, value = parse_attribute_name_and_value(
                data,
                attr,
            )

            if name is None:
                continue

            lname = name.lower()

            if lname == b"id" and value is not None:
                id_value = value.decode(
                    "utf-8",
                    errors="replace",
                )

            elif lname == b"class" and value is not None:
                class_value = value.decode(
                    "utf-8",
                    errors="replace",
                )

        raw_nodes.append(
            {
                "node": node,
                "tag_name": tag_name,
                "start_byte": node.start_byte,
                "end_byte": node.end_byte,
                "id_value": id_value,
                "class_value": class_value,
            }
        )

    # -----------------------------------------------------------------------
    # Establish candidate-tree relationships.
    #
    # We cannot simply use Tree-sitter parent pointers because structural
    # elements have been excluded. Find the nearest enclosing candidate.
    # -----------------------------------------------------------------------

    # Sort by start ascending and end descending.
    ordered = sorted(
        range(len(raw_nodes)),
        key=lambda i: (
            raw_nodes[i]["start_byte"],
            -raw_nodes[i]["end_byte"],
        ),
    )

    parent_by_raw_index = {}

    stack = []

    for raw_i in ordered:
        start = raw_nodes[raw_i]["start_byte"]
        end = raw_nodes[raw_i]["end_byte"]

        while stack:
            parent_i = stack[-1]

            parent_start = raw_nodes[parent_i]["start_byte"]
            parent_end = raw_nodes[parent_i]["end_byte"]

            if (
                parent_start <= start
                and end <= parent_end
            ):
                break

            stack.pop()

        parent_by_raw_index[raw_i] = (
            stack[-1] if stack else None
        )

        stack.append(raw_i)

    # -----------------------------------------------------------------------
    # Assign stable candidate indices.
    # -----------------------------------------------------------------------

    raw_to_candidate = {}

    result = []

    for candidate_index, raw_i in enumerate(ordered):
        raw_to_candidate[raw_i] = candidate_index

        parent_raw = parent_by_raw_index[raw_i]

        parent = (
            raw_to_candidate[parent_raw]
            if parent_raw is not None
            else None
        )

        depth = 0

        if parent is not None:
            depth = result[parent].depth + 1

        r = raw_nodes[raw_i]

        result.append(
            XhtmlSubtree(
                index=candidate_index,
                tag_name=r["tag_name"],
                start_byte=r["start_byte"],
                end_byte=r["end_byte"],
                parent=parent,
                depth=depth,
                id_value=r["id_value"],
                class_value=r["class_value"],
            )
        )

    return result


# ---------------------------------------------------------------------------
# Human-readable description
# ---------------------------------------------------------------------------

def describe_subtree(e: XhtmlSubtree) -> str:
    tag = e.tag_name.decode(
        "utf-8",
        errors="replace",
    )

    text = f"<{tag}"

    if e.id_value:
        text += f' id="{e.id_value}"'

    if e.class_value:
        text += f' class="{e.class_value}"'

    text += ">"

    return text


# ---------------------------------------------------------------------------
# Determine which candidates are actually still present.
#
# If candidate 10 was removed, every descendant of candidate 10 has also
# disappeared. Such descendants must NOT be tested independently.
# ---------------------------------------------------------------------------

def active_subtree_candidates(
    elements: list[XhtmlSubtree],
    removed: set[int],
) -> list[int]:
    """
    Return candidates that still physically exist.

    A candidate is active if none of its ancestors has been removed.
    """

    active = []

    for e in elements:
        p = e.parent

        while p is not None:
            if p in removed:
                break

            p = elements[p].parent

        else:
            # No removed ancestor.
            if e.index not in removed:
                active.append(e.index)

    return active


# ---------------------------------------------------------------------------
# Byte deletion
# ---------------------------------------------------------------------------

def remove_xhtml_subtrees(
    data: bytes,
    elements: list[XhtmlSubtree],
    removed: set[int],
) -> bytes:
    """
    Delete complete element byte ranges.

    Descendants of already removed elements are ignored because their bytes
    have already been deleted as part of the ancestor.
    """

    ranges = []

    for i in removed:
        e = elements[i]

        # If an ancestor is also removed, this range is redundant.
        p = e.parent

        ancestor_removed = False

        while p is not None:
            if p in removed:
                ancestor_removed = True
                break

            p = elements[p].parent

        if ancestor_removed:
            continue

        ranges.append(
            (e.start_byte, e.end_byte)
        )

    return apply_deletions(
        data,
        ranges,
    )


@dataclass
class XhtmlRegion:
    index: int
    tag_name: str
    start_byte: int
    end_byte: int
    parent_index: int | None
    depth: int
    kind: str


def find_xhtml_regions(data: bytes) -> list[XhtmlRegion]:
    """
    Find removable/restorable XHTML element regions.

    Every region is represented by its original byte range.

    Paired elements:
        <div> ... </div>

    Void/leaf elements:
        <img ...>
        <br>
        <meta ...>
        <link ...>

    We deliberately exclude html/head/body/script/style at this stage.
    """
    parser = make_parser()
    tree = parser.parse(data)

    raw_elements = []

    for node in walk(tree.root_node):
        if node.type != "element":
            continue

        start_tag = direct_child(node, "start_tag")
        if start_tag is None:
            continue

        tag_name = get_tag_name(data, start_tag)
        if not tag_name:
            continue

        tag_lower = tag_name.lower()

        if tag_lower in {
            "html",
            "head",
            "body",
            "script",
            "style",
        }:
            continue

        end_tag = direct_child(node, "end_tag")

        if end_tag is not None:
            start_byte = node.start_byte
            end_byte = node.end_byte
            kind = "element"
        else:
            # Tree-sitter represents void/leaf elements as elements
            # without an end_tag. The node range is still the exact
            # original byte range.
            start_byte = node.start_byte
            end_byte = node.end_byte
            kind = "leaf"

        if end_byte <= start_byte:
            continue

        raw_elements.append(
            {
                "node": node,
                "tag_name": tag_name,
                "start_byte": start_byte,
                "end_byte": end_byte,
                "depth": 0,
                "kind": kind,
            }
        )

    # Sort enclosing elements before their descendants.
    raw_elements.sort(
        key=lambda x: (
            x["start_byte"],
            -x["end_byte"],
        )
    )

    regions = []

    for raw in raw_elements:
        parent_index = None
        parent_span = None

        for previous in regions:
            if (
                previous.start_byte <= raw["start_byte"]
                and raw["end_byte"] <= previous.end_byte
                and (
                    previous.start_byte < raw["start_byte"]
                    or raw["end_byte"] < previous.end_byte
                )
            ):
                span = previous.end_byte - previous.start_byte

                if parent_span is None or span < parent_span:
                    parent_index = previous.index
                    parent_span = span

        depth = (
            0
            if parent_index is None
            else regions[parent_index].depth + 1
        )

        region = XhtmlRegion(
            index=len(regions),
            tag_name=raw["tag_name"],
            start_byte=raw["start_byte"],
            end_byte=raw["end_byte"],
            parent_index=parent_index,
            depth=depth,
            kind=raw["kind"],
        )

        regions.append(region)

    return regions


def find_structural_skeleton(data: bytes) -> bytes:
    """
    Construct a byte-preserving minimal XHTML skeleton.

    We retain the original bytes for:

        <html ...>
        <head ...>
        </head>
        <body ...>
        </body>
        </html>

    and remove everything between those structural tags.

    This is deliberately NOT an XML/HTML serialization operation.
    All retained bytes come directly from the original XHTML.
    """
    parser = make_parser()
    tree = parser.parse(data)

    html_element = None

    for node in walk(tree.root_node):
        if node.type != "element":
            continue

        start_tag = direct_child(node, "start_tag")
        if start_tag is None:
            continue

        tag_name = get_tag_name(data, start_tag)

        if tag_name and tag_name.lower() == "html":
            html_element = node
            break

    if html_element is None:
        raise RuntimeError(
            "Could not find <html> element while constructing "
            "XHTML skeleton."
        )

    html_start = direct_child(html_element, "start_tag")
    html_end = direct_child(html_element, "end_tag")

    if html_start is None or html_end is None:
        raise RuntimeError(
            "The XHTML <html> element does not have both start/end tags."
        )

    head_element = None
    body_element = None

    for child in html_element.children:
        if child.type != "element":
            continue

        child_start = direct_child(child, "start_tag")
        if child_start is None:
            continue

        child_name = get_tag_name(data, child_start)

        if not child_name:
            continue

        child_name = child_name.lower()

        if child_name == "head":
            head_element = child
        elif child_name == "body":
            body_element = child

    if head_element is None or body_element is None:
        raise RuntimeError(
            "Could not find both <head> and <body> inside <html>."
        )

    head_start = direct_child(head_element, "start_tag")
    head_end = direct_child(head_element, "end_tag")
    body_start = direct_child(body_element, "start_tag")
    body_end = direct_child(body_element, "end_tag")

    if (
        head_start is None
        or head_end is None
        or body_start is None
        or body_end is None
    ):
        raise RuntimeError(
            "XHTML <head>/<body> elements are missing "
            "required start/end tags."
        )

    # Keep everything outside <html>, including XML declarations,
    # DOCTYPEs, comments, etc.
    prefix = data[:html_start.start_byte]
    suffix = data[html_end.end_byte:]

    skeleton = bytearray()

    skeleton += prefix

    skeleton += data[
        html_start.start_byte:
        html_start.end_byte
    ]

    skeleton += data[
        head_start.start_byte:
        head_start.end_byte
    ]

    skeleton += data[
        head_end.start_byte:
        head_end.end_byte
    ]

    skeleton += data[
        body_start.start_byte:
        body_start.end_byte
    ]

    skeleton += data[
        body_end.start_byte:
        body_end.end_byte
    ]

    skeleton += data[
        html_end.start_byte:
        html_end.end_byte
    ]

    skeleton += suffix

    return bytes(skeleton)


def restore_regions(
        original: bytes,
        regions: list[XhtmlRegion],
        restored: set[int],
    ) -> bytes:
    """
    Construct a candidate XHTML by starting from the structural
    skeleton and restoring complete original region byte ranges.

    A region can only be restored if all of its ancestors are also
    restored. This prevents detached nested fragments.
    """
    if not restored:
        return find_structural_skeleton(original)

    effective = set()

    for index in restored:
        region = regions[index]

        parent = region.parent_index
        valid = True

        while parent is not None:
            if parent not in restored:
                valid = False
                break

            parent = regions[parent].parent_index

        if valid:
            effective.add(index)

    ranges = [
        (
            regions[index].start_byte,
            regions[index].end_byte,
        )
        for index in effective
    ]

    # Remove ranges completely contained in another restored range.
    ranges.sort(key=lambda x: (x[0], -x[1]))

    non_overlapping = []

    for start, end in ranges:
        if non_overlapping:
            previous_start, previous_end = non_overlapping[-1]

            if end <= previous_end:
                continue

        non_overlapping.append((start, end))

    # Reconstruct by taking the original bytes for restored regions
    # and the structural skeleton everywhere else.
    #
    # We do this by taking the original document and replacing the
    # interior of <head>/<body> recursively. The simplest robust
    # approach is to use the skeleton and insert restored regions at
    # their original positions relative to the skeleton.
    skeleton = find_structural_skeleton(original)

    #
    # Instead of trying to map offsets into the skeleton, reconstruct
    # the document from original byte intervals.
    #
    parser = make_parser()
    tree = parser.parse(original)

    html_element = None

    for node in walk(tree.root_node):
        if node.type != "element":
            continue

        start_tag = direct_child(node, "start_tag")
        if start_tag is None:
            continue

        tag_name = get_tag_name(original, start_tag)

        if tag_name and tag_name.lower() == "html":
            html_element = node
            break

    if html_element is None:
        return skeleton

    html_start = direct_child(html_element, "start_tag")
    html_end = direct_child(html_element, "end_tag")

    head_element = None
    body_element = None

    for child in html_element.children:
        if child.type != "element":
            continue

        start_tag = direct_child(child, "start_tag")
        if start_tag is None:
            continue

        name = get_tag_name(original, start_tag)

        if not name:
            continue

        if name.lower() == "head":
            head_element = child
        elif name.lower() == "body":
            body_element = child

    if (
        html_start is None
        or html_end is None
        or head_element is None
        or body_element is None
    ):
        return skeleton

    head_start = direct_child(head_element, "start_tag")
    head_end = direct_child(head_element, "end_tag")
    body_start = direct_child(body_element, "start_tag")
    body_end = direct_child(body_element, "end_tag")

    structural_ranges = [
        (0, html_start.end_byte),
        (head_start.start_byte, head_start.end_byte),
        (head_end.start_byte, head_end.end_byte),
        (body_start.start_byte, body_start.end_byte),
        (body_end.start_byte, body_end.end_byte),
        (html_end.start_byte, len(original)),
    ]

    # The above structural ranges don't account for content between
    # the structural tags, so construct the document explicitly.
    parts = []

    parts.append(
        original[:html_start.end_byte]
    )

    def append_children(container_start, container_end):
        cursor = container_start

        children = [
            region
            for region in regions
            if region.start_byte >= container_start
            and region.end_byte <= container_end
            and region.parent_index is not None
        ]

        children.sort(
            key=lambda r: (r.start_byte, -r.end_byte)
        )

        top_level = []

        for region in children:
            parent = region.parent_index

            if parent is None:
                continue

            parent_region = regions[parent]

            if (
                parent_region.start_byte >= container_start
                and parent_region.end_byte <= container_end
            ):
                continue

            # This is not actually a direct child of the container.
            continue

        #
        # Easier and more reliable: find direct candidate children by
        # checking parent relationships.
        #
        direct_regions = []

        for region in regions:
            if (
                region.start_byte >= container_start
                and region.end_byte <= container_end
                and region.parent_index is None
            ):
                direct_regions.append(region)

        # Candidate roots inside head/body are those whose parent
        # candidate is outside the candidate set for this container.
        for region in regions:
            if not (
                region.start_byte >= container_start
                and region.end_byte <= container_end
            ):
                continue

            if region.parent_index is None:
                direct_regions.append(region)
                continue

            parent = regions[region.parent_index]

            if not (
                parent.start_byte >= container_start
                and parent.end_byte <= container_end
            ):
                direct_regions.append(region)

        direct_regions.sort(
            key=lambda r: r.start_byte
        )

        cursor = container_start

        for region in direct_regions:
            if region.start_byte < cursor:
                continue

            # Preserve original bytes before this candidate.
            # However, for the actual content region we want to remove
            # all non-restored bytes. Therefore only emit bytes that
            # belong to structural whitespace is intentionally NOT
            # preserved here.
            #
            # The candidate itself is restored only if selected.
            if region.index in effective:
                parts.append(
                    original[
                        region.start_byte:
                        region.end_byte
                    ]
                )

            cursor = max(cursor, region.end_byte)

    # The helper above is intentionally not used for the final
    # reconstruction because preserving arbitrary whitespace would
    # make the "empty" baseline non-empty. Build head/body directly.
    #
    # HEAD
    parts.append(
        original[
            head_start.start_byte:
            head_start.end_byte
        ]
    )

    head_regions = [
        r
        for r in regions
        if (
            r.start_byte >= head_start.end_byte
            and r.end_byte <= head_end.start_byte
            and (
                r.parent_index is None
                or not (
                    regions[r.parent_index].start_byte
                    >= head_start.end_byte
                    and regions[r.parent_index].end_byte
                    <= head_end.start_byte
                )
            )
        )
    ]

    head_regions.sort(key=lambda r: r.start_byte)

    for region in head_regions:
        if region.index in effective:
            parts.append(
                original[
                    region.start_byte:
                    region.end_byte
                ]
            )

    parts.append(
        original[
            head_end.start_byte:
            head_end.end_byte
        ]
    )

    # BODY
    parts.append(
        original[
            body_start.start_byte:
            body_start.end_byte
        ]
    )

    body_regions = [
        r
        for r in regions
        if (
            r.start_byte >= body_start.end_byte
            and r.end_byte <= body_end.start_byte
            and (
                r.parent_index is None
                or not (
                    regions[r.parent_index].start_byte
                    >= body_start.end_byte
                    and regions[r.parent_index].end_byte
                    <= body_end.start_byte
                )
            )
        )
    ]

    body_regions.sort(key=lambda r: r.start_byte)

    for region in body_regions:
        if region.index in effective:
            parts.append(
                original[
                    region.start_byte:
                    region.end_byte
                ]
            )

    parts.append(
        original[
            body_end.start_byte:
            body_end.end_byte
        ]
    )

    parts.append(
        original[
            html_end.start_byte:
            html_end.end_byte
        ]
    )

    parts.append(
        original[
            html_end.end_byte:
        ]
    )

    return b"".join(parts)


# ============================================================================
# Search engine
# ============================================================================

class HierarchicalXhtmlRestorer:
    """
    Minimize an XHTML document starting from a known-GOOD empty
    structural skeleton and restoring original XHTML regions.

    The search is deliberately GOOD-biased:

        GOOD + restoration -> GOOD  => keep restoration
        GOOD + restoration -> BAD   => reject restoration

    No attempt is made to discover GOOD states from arbitrary BAD
    states.
    """

    def __init__(
        self,
        original: bytes,
        test_candidate,
        seed: int = 12345,
        checkpoint_path: Path | None = None,
    ):
        self.original = original
        self.test_candidate = test_candidate
        self.rng = random.Random(seed)
        self.checkpoint_path = checkpoint_path

        self.regions = find_xhtml_regions(original)

        self.restored: set[int] = set()
        self.test_cache: dict[frozenset[int], bool] = {}

    def test(self, restored: set[int]) -> bool:
        key = frozenset(restored)

        cached = self.test_cache.get(key)
        if cached is not None:
            return cached

        candidate = restore_regions(
            self.original,
            self.regions,
            restored,
        )

        good = self.test_candidate(candidate)

        self.test_cache[key] = good

        print(
            f"[TEST] restored={len(restored):4d} "
            f"regions -> {'GOOD' if good else 'BAD'}"
        )

        return good

    def save_checkpoint(self):
        if self.checkpoint_path is None:
            return

        payload = {
            "restored": sorted(self.restored),
            "seed": self.rng.getstate(),
        }

        self.checkpoint_path.write_text(
            repr(payload),
            encoding="utf-8",
        )

    def initial_good_baseline(self):
        """
        Verify that the structural skeleton is GOOD.

        If it isn't, use the completely absent XHTML as the GOOD
        baseline. The caller's test_candidate() is responsible for
        implementing that fallback.
        """
        if self.test(set()):
            print("[INFO] Empty structural XHTML skeleton is GOOD.")
            return

        raise RuntimeError(
            "The minimal XHTML structural skeleton is BAD. "
            "The caller should fall back to testing the XHTML "
            "file completely absent."
        )

    def top_level_regions(self) -> list[int]:
        """
        Regions that do not have another candidate region as parent.
        """
        return [
            r.index
            for r in self.regions
            if r.parent_index is None
        ]

    def descendants(self, index: int) -> list[int]:
        result = []

        for region in self.regions:
            parent = region.parent_index

            while parent is not None:
                if parent == index:
                    result.append(region.index)
                    break

                parent = self.regions[parent].parent_index

        return result

    def restore_batches(
        self,
        indices: list[int],
        batch_size: int,
    ):
        """
        Randomly try batches of regions.

        A batch is kept only if the resulting EPUB remains GOOD.
        Rejected batches are recursively subdivided.
        """
        if not indices:
            return

        self.rng.shuffle(indices)

        pos = 0

        while pos < len(indices):
            batch = indices[pos:pos + batch_size]
            pos += batch_size

            candidate = set(self.restored)
            candidate.update(batch)

            if self.test(candidate):
                self.restored = candidate

                print(
                    f"[KEEP] restored batch of {len(batch)} "
                    f"(total={len(self.restored)})"
                )

                self.save_checkpoint()

            elif len(batch) > 1:
                midpoint = len(batch) // 2

                left = batch[:midpoint]
                right = batch[midpoint:]

                self.restore_batches(left, 1)
                self.restore_batches(right, 1)

    def minimize(self):
        self.initial_good_baseline()

        top = self.top_level_regions()

        print(
            f"[INFO] Found {len(self.regions)} candidate XHTML "
            f"regions."
        )

        print(
            f"[INFO] Starting with {len(top)} top-level regions."
        )

        #
        # First pass: try reasonably large random batches.
        #
        if top:
            batch_size = max(1, len(top) // 8)

            print(
                f"[INFO] Initial restoration batch size: "
                f"{batch_size}"
            )

            self.restore_batches(
                list(top),
                batch_size,
            )

        #
        # Second pass: recursively inspect every retained subtree.
        #
        changed = True

        while changed:
            changed = False

            current = sorted(
                self.restored,
                key=lambda i: self.regions[i].depth,
            )

            for parent_index in current:
                children = [
                    r.index
                    for r in self.regions
                    if r.parent_index == parent_index
                ]

                if not children:
                    continue

                before = len(self.restored)

                self.restore_batches(
                    children,
                    max(1, len(children) // 4),
                )

                if len(self.restored) != before:
                    changed = True

        #
        # Final 1-minimality pass.
        #
        print("[INFO] Starting final 1-minimality pass.")

        for index in sorted(
            list(self.restored),
            key=lambda i: self.regions[i].depth,
            reverse=True,
        ):
            candidate = set(self.restored)
            candidate.remove(index)

            #
            # Removing a parent implicitly removes all of its
            # descendants from the generated document, so don't
            # accidentally treat those descendants as independently
            # retained.
            descendants = self.descendants(index)
            candidate.difference_update(descendants)

            if self.test(candidate):
                self.restored = candidate

                print(
                    f"[REMOVE] region {index} "
                    f"<{self.regions[index].tag_name}> "
                    f"is not required."
                )

            self.save_checkpoint()

        return self.restored


# ============================================================================
# Main minimization function
# ============================================================================

def minimize_xhtml_subtrees(
        normalized_epub,
        html_path,
        output_epub,
        reader,
        timeout=DEFAULT_TIMEOUT,
        interval=DEFAULT_INTERVAL,
        idle_threshold=DEFAULT_IDLE_THRESHOLD,
        idle_samples=DEFAULT_IDLE_SAMPLES,
        seed=12345,
        checkpoint_path=None,
    ):
    """
    Hierarchically minimize one XHTML file.

    The target XHTML is first reduced to a minimal structural skeleton.
    The minimizer then restores original XHTML subtrees while retaining
    only restorations that keep the EPUB GOOD.
    """
    normalized_epub = Path(normalized_epub).resolve()

    epub_root = normalized_epub.parent

    html_path = Path(html_path)

    if not html_path.is_absolute():
        html_path = (
            epub_root / html_path
        ).resolve()
    else:
        html_path = html_path.resolve()

    if not normalized_epub.exists():
        raise RuntimeError(
            "Normalized EPUB does not exist: "
            f"{str(normalized_epub)!r}"
        )

    if not html_path.exists():
        raise RuntimeError(
            "XHTML file does not exist: "
            f"{str(html_path)!r}"
        )

    try:
        html_relpath = html_path.relative_to(epub_root)
    except ValueError:
        raise RuntimeError(
            "XHTML file is not inside the normalized EPUB directory:\n"
            f"  EPUB root: {str(epub_root)!r}\n"
            f"  XHTML:     {str(html_path)!r}"
        )

    original = html_path.read_bytes()

    print()
    print("[INFO] XHTML filesystem path:")
    print(f"       {str(html_path)!r}")

    print("[INFO] XHTML EPUB path:")
    print(f"       {str(html_relpath)!r}")

    print(f"[INFO] XHTML size: {len(original):,} bytes")

    #
    # Candidate EPUB creation.
    #
    def make_candidate(candidate_xhtml: bytes, absent=False):
        print("")
        with tempfile.TemporaryDirectory(
            prefix="epub-subtree-"
        ) as tmp:
            tmpdir = Path(tmp)

            shutil.copytree(
                epub_root,
                tmpdir,
                dirs_exist_ok=True,
            )

            candidate_file = tmpdir / html_relpath

            if absent:
                if candidate_file.exists():
                    candidate_file.unlink()
            else:
                candidate_file.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                candidate_file.write_bytes(
                    candidate_xhtml
                )

            candidate_epub = (
                tmpdir.parent
                / f"{tmpdir.name}.epub"
            )

            pack_epub(
                tmpdir,
                candidate_epub,
            )

            try:
                return benchmark_epub(
                    candidate_epub,
                    reader,
                    timeout,
                    interval,
                    idle_threshold,
                    idle_samples,
                )
            finally:
                try:
                    candidate_epub.unlink()
                except FileNotFoundError:
                    pass

    #
    # We know from the experiment that completely removing the XHTML
    # file is GOOD. Verify that once so the search has a guaranteed
    # GOOD endpoint.
    #
    print()
    print("[INFO] Verifying known-good baseline: XHTML absent...")

    absent_good = make_candidate(
        b"",
        absent=True,
    )

    if not absent_good:
        raise RuntimeError(
            "Unexpected result: completely removing the target XHTML "
            "file did not make the EPUB GOOD."
        )

    print(
        "[GOOD] XHTML completely absent is confirmed GOOD."
    )

    #
    # Candidate test function.
    #
    def test_candidate(candidate_xhtml: bytes):
        return make_candidate(
            candidate_xhtml,
            absent=False,
        )

    #
    # First establish whether our byte-preserving structural skeleton
    # is itself GOOD.
    #
    skeleton = find_structural_skeleton(original)

    print()
    print(
        "[INFO] Structural skeleton size: "
        f"{len(skeleton):,} bytes"
    )

    skeleton_good = test_candidate(skeleton)

    if skeleton_good:
        print(
            "[GOOD] Structural skeleton is GOOD; "
            "starting hierarchical restoration."
        )
    else:
        print(
            "[WARN] Structural skeleton is BAD."
        )
        print(
            "[WARN] Falling back to the known-good absent-XHTML "
            "baseline."
        )

        #
        # If even the empty html/head/body skeleton is BAD, the
        # culprit is in the structural markup itself. At this point
        # the subtree restoration model cannot safely reconstruct it
        # without restoring the structural tags too.
        #
        # We therefore start by treating the complete original XHTML
        # as one candidate and use binary byte-range restoration below.
        #
        return minimize_xhtml_bytes_from_absent(
            normalized_epub=normalized_epub,
            html_relpath=html_relpath,
            original=original,
            output_epub=output_epub,
            reader=reader,
            timeout=timeout,
            interval=interval,
            idle_threshold=idle_threshold,
            idle_samples=idle_samples,
            seed=seed,
        )

    minimizer = HierarchicalXhtmlRestorer(
        original=original,
        test_candidate=test_candidate,
        seed=seed,
        checkpoint_path=checkpoint_path,
    )

    restored = minimizer.minimize()

    final_xhtml = restore_regions(
        original,
        minimizer.regions,
        restored,
    )

    #
    # Write final minimized EPUB.
    #
    with tempfile.TemporaryDirectory(
        prefix="epub-final-"
    ) as tmp:
        tmpdir = Path(tmp)

        shutil.copytree(
            epub_root,
            tmpdir,
            dirs_exist_ok=True,
        )

        final_file = tmpdir / html_relpath

        final_file.write_bytes(
            final_xhtml
        )

        pack_epub(
            tmpdir,
            output_epub,
        )

    print()
    print("=" * 72)
    print("XHTML SUBTREE MINIMIZATION COMPLETE")
    print("=" * 72)
    print(f"Original XHTML:  {len(original):,} bytes")
    print(f"Final XHTML:     {len(final_xhtml):,} bytes")
    print(
        f"Reduction:       "
        f"{len(original) - len(final_xhtml):,} bytes"
    )
    print(f"Restored regions: {len(restored)}")
    print(f"Output EPUB:     {str(output_epub)!r}")

    return output_epub


def minimize_xhtml_bytes_from_absent(
        normalized_epub,
        html_relpath,
        original,
        output_epub,
        reader,
        timeout,
        interval,
        idle_threshold,
        idle_samples,
        seed=12345,
    ):
    """
    Fallback for the unusual case where even the empty structural
    XHTML skeleton is BAD.

    Start from XHTML completely absent (known GOOD), then restore
    original byte ranges hierarchically.

    This is intentionally byte-oriented so that even malformed
    structural markup can be isolated.
    """
    rng = random.Random(seed)

    normalized_epub = Path(normalized_epub).resolve()
    epub_root = normalized_epub.parent

    def test_bytes(candidate):
        with tempfile.TemporaryDirectory(
            prefix="epub-byte-"
        ) as tmp:
            tmpdir = Path(tmp)

            shutil.copytree(
                epub_root,
                tmpdir,
                dirs_exist_ok=True,
            )

            candidate_file = tmpdir / html_relpath

            candidate_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            candidate_file.write_bytes(candidate)

            candidate_epub = (
                tmpdir.parent
                / f"{tmpdir.name}.epub"
            )

            pack_epub(
                tmpdir,
                candidate_epub,
            )

            try:
                return benchmark_epub(
                    candidate_epub,
                    reader,
                    timeout,
                    interval,
                    idle_threshold,
                    idle_samples,
                )
            finally:
                try:
                    candidate_epub.unlink()
                except FileNotFoundError:
                    pass

    def make_candidate(ranges):
        output = bytearray()

        for start, end in sorted(ranges):
            output.extend(
                original[start:end]
            )

        return bytes(output)

    #
    # We need a mapping from original byte ranges to the resulting
    # candidate. Start with the whole file as one range.
    #
    ranges = [(0, len(original))]

    #
    # First recursively split ranges. A range is retained only if
    # restoring it keeps the EPUB GOOD.
    #
    def search_range(start, end):
        if end <= start:
            return []

        candidate = original[start:end]

        if test_bytes(candidate):
            print(
                f"[KEEP] bytes {start}:{end} "
                f"({end - start:,} bytes)"
            )
            return [(start, end)]

        if end - start == 1:
            print(
                f"[DROP] byte {start}: "
                f"{original[start:start+1].hex()}"
            )
            return []

        midpoint = start + ((end - start) // 2)

        left = search_range(start, midpoint)
        right = search_range(midpoint, end)

        return left + right

    #
    # IMPORTANT:
    #
    # Testing arbitrary ranges in isolation can miss interactions.
    # Therefore use randomized groups first.
    #
    all_indices = list(range(len(original)))

    rng.shuffle(all_indices)

    kept = []

    #
    # Large chunks first.
    #
    chunk_size = max(
        1,
        len(original) // 32,
    )

    chunks = [
        all_indices[i:i + chunk_size]
        for i in range(0, len(all_indices), chunk_size)
    ]

    for chunk in chunks:
        chunk.sort()

        candidate = b"".join(
            original[i:i + 1]
            for i in chunk
        )

        if test_bytes(candidate):
            kept.extend(chunk)

            print(
                f"[KEEP] random byte group: "
                f"{len(chunk):,} bytes"
            )

    #
    # Fall back to deterministic interval subdivision for anything
    # not established as useful.
    #
    kept_set = set(kept)

    intervals = []

    i = 0

    while i < len(original):
        if i in kept_set:
            i += 1
            continue

        start = i

        while (
            i < len(original)
            and i not in kept_set
        ):
            i += 1

        intervals.append(
            (start, i)
        )

    for start, end in intervals:
        recovered = search_range(start, end)
        kept.extend(
            byte_index
            for a, b in recovered
            for byte_index in range(a, b)
        )

    kept = sorted(set(kept))

    final_xhtml = bytes(
        original[i:i + 1]
        for i in kept
    )

    #
    # Write result.
    #
    with tempfile.TemporaryDirectory(
        prefix="epub-final-byte-"
    ) as tmp:
        tmpdir = Path(tmp)

        shutil.copytree(
            epub_root,
            tmpdir,
            dirs_exist_ok=True,
        )

        final_file = tmpdir / html_relpath
        final_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        final_file.write_bytes(
            final_xhtml
        )

        pack_epub(
            tmpdir,
            output_epub,
        )

    print()
    print("=" * 72)
    print("BYTE-LEVEL XHTML MINIMIZATION COMPLETE")
    print("=" * 72)
    print(f"Original XHTML: {len(original):,} bytes")
    print(f"Final XHTML:    {len(final_xhtml):,} bytes")
    print(
        f"Reduction:      "
        f"{len(original) - len(final_xhtml):,} bytes"
    )
    print(f"Output EPUB:    {str(output_epub)!r}")

    return output_epub


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

    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducible minimization",
    )

    parser.add_argument(
        "--checkpoint",
        type=str,
        default=None,
        help="Write minimization checkpoint to this JSON file",
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

    r'''
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
    '''

    r'''
    # generic XHTML minimization.
    result = minimize_xhtml_elements(
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
    '''

    r'''
    minimize_xhtml_subtrees(
        normalized_epub=normalized_epub,
        html_path=html_path,
        output_epub=final_output,
        reader=args.reader,
        timeout=args.timeout,
        interval=args.interval,
        idle_threshold=args.idle_threshold,
        idle_samples=args.idle_samples,
        seed=args.seed,
        checkpoint_path=(
            Path(args.checkpoint)
            if args.checkpoint
            else None
        ),
    )
    '''

    # ------------------------------------------------------------------
    # Stage 2:
    # XHTML subtree minimization.
    # ------------------------------------------------------------------

    normalized_epub = output_dir / "_normalized.epub"

    pack_epub(
        normalized_directory,
        normalized_epub,
    )

    html_path = normalized_directory / target_relative_path

    final_output = output_dir / "minimized.epub"

    print()
    print("=" * 72)
    print("STAGE 2: XHTML SUBTREE MINIMIZATION")
    print("=" * 72)
    print(f"Normalized EPUB: {normalized_epub!r}")
    print(f"Target XHTML:    {html_path!r}")
    print(f"Target EPUB path: {target_relative_path!r}")
    print(f"Output EPUB:     {final_output!r}")

    minimize_xhtml_subtrees(
        normalized_epub=normalized_epub,
        html_path=html_path,
        output_epub=final_output,
        reader=args.epub_reader,
        timeout=args.timeout,
        interval=args.interval,
        idle_threshold=args.idle_threshold,
        idle_samples=args.idle_samples,
        seed=args.seed,
        checkpoint_path=(
            Path(args.checkpoint)
            if args.checkpoint
            else None
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
