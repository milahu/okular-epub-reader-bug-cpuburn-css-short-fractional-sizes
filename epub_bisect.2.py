#!/usr/bin/env python3

"""
Bisect a problematic XHTML file inside an EPUB.

The EPUB must first be unpacked into a directory.

Example:

    python epub_bisect.py my_epub_unpacked/

Output:

    epub_bisect/
        test-0001.epub
        test-0002.epub
        ...
        manifest.csv

The script targets:

    OEBPS/appendix-001.xhtml

It creates EPUBs containing progressively larger portions of the
document body, while keeping the XHTML itself structurally valid.

For a more detailed recursive search:

    python epub_bisect.py my_epub_unpacked/ --recursive

Requirements:
    Python 3.9+
"""

from __future__ import annotations

import argparse
import csv
import copy
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


TARGET = "OEBPS/appendix-001.xhtml"


# XHTML namespace
XHTML_NS = "http://www.w3.org/1999/xhtml"

ET.register_namespace("", XHTML_NS)


def local_name(tag: str) -> str:
    """Return the local part of an XML tag."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def find_body(root: ET.Element) -> ET.Element:
    """Find the XHTML <body> element."""
    for element in root.iter():
        if local_name(element.tag) == "body":
            return element

    raise RuntimeError("Could not find <body> element")


def parse_xhtml(path: Path) -> ET.ElementTree:
    """Parse XHTML as XML."""
    try:
        return ET.parse(path)
    except ET.ParseError as e:
        raise RuntimeError(
            f"Could not parse {path} as XML/XHTML:\n{e}\n\n"
            "The file may itself be malformed. In that case use "
            "--raw-lines instead."
        ) from e


def serialize_tree(tree: ET.ElementTree) -> bytes:
    """Serialize XHTML XML tree."""
    return ET.tostring(
        tree.getroot(),
        encoding="utf-8",
        xml_declaration=True,
    )


def write_epub(
    unpacked_dir: Path,
    output_epub: Path,
    xhtml_bytes: bytes,
) -> None:
    """
    Create a new EPUB by copying everything from unpacked_dir,
    replacing appendix-001.xhtml.
    """

    target_relative = Path(TARGET)

    output_epub.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp:
        temp_dir = Path(temp)

        # Copy the complete EPUB directory tree.
        copied_target = temp_dir / target_relative
        copied_target.parent.mkdir(parents=True, exist_ok=True)

        for src in unpacked_dir.rglob("*"):
            rel = src.relative_to(unpacked_dir)
            dst = temp_dir / rel

            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

        # Replace the problematic XHTML.
        copied_target.write_bytes(xhtml_bytes)

        # EPUB requires mimetype to be the first ZIP entry and uncompressed.
        mimetype = temp_dir / "mimetype"

        with zipfile.ZipFile(
            output_epub,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as zf:

            if mimetype.exists():
                zf.write(
                    mimetype,
                    "mimetype",
                    compress_type=zipfile.ZIP_STORED,
                )

            for path in sorted(temp_dir.rglob("*")):
                if path.is_dir():
                    continue

                rel = path.relative_to(temp_dir).as_posix()

                if rel == "mimetype":
                    continue

                zf.write(
                    path,
                    rel,
                    compress_type=zipfile.ZIP_DEFLATED,
                )


def make_prefix_variants(
    tree: ET.ElementTree,
    count: int,
) -> list[tuple[str, bytes]]:
    """
    Generate cumulative prefixes of the body's direct child elements.

    Example for 10 children:

        10%
        20%
        ...
        100%

    Every generated XHTML remains structurally valid.
    """

    root = tree.getroot()
    body = find_body(root)

    children = list(body)

    if not children:
        raise RuntimeError("<body> contains no child elements")

    variants = []

    for i in range(1, count + 1):
        n = round(len(children) * i / count)

        # Ensure the final variant contains everything.
        n = min(n, len(children))

        new_root = copy.deepcopy(root)
        new_body = find_body(new_root)

        # Remove all body children.
        for child in list(new_body):
            new_body.remove(child)

        # Add the selected prefix.
        for child in children[:n]:
            new_body.append(copy.deepcopy(child))

        new_tree = ET.ElementTree(new_root)

        percentage = 100.0 * n / len(children)

        name = f"prefix-{i:03d}-{percentage:06.2f}pct"

        variants.append(
            (
                name,
                serialize_tree(new_tree),
            )
        )

    return variants


def make_half_variants(
    tree: ET.ElementTree,
) -> list[tuple[str, bytes]]:
    """
    Generate four useful variants:

        first half
        second half
        first 75%
        last 75%

    These are particularly useful after identifying a suspicious
    region with the prefix tests.
    """

    root = tree.getroot()
    body = find_body(root)
    children = list(body)

    if not children:
        raise RuntimeError("<body> contains no child elements")

    n = len(children)
    midpoint = n // 2

    ranges = [
        ("first-half", 0, midpoint),
        ("second-half", midpoint, n),
        ("first-75pct", 0, round(n * 0.75)),
        ("last-75pct", round(n * 0.25), n),
    ]

    variants = []

    for name, start, end in ranges:
        new_root = copy.deepcopy(root)
        new_body = find_body(new_root)

        for child in list(new_body):
            new_body.remove(child)

        for child in children[start:end]:
            new_body.append(copy.deepcopy(child))

        variants.append(
            (
                name,
                serialize_tree(ET.ElementTree(new_root)),
            )
        )

    return variants


def make_recursive_variants(
    tree: ET.ElementTree,
) -> list[tuple[str, bytes]]:
    """
    Generate variants for every element that has children.

    For each element with children, produce:

        element-first-half
        element-second-half

    The surrounding document structure is preserved.

    This is useful if the problem is buried deep inside a large
    <div>, <section>, <table>, etc.
    """

    original_root = tree.getroot()
    variants = []

    counter = 0

    for element in original_root.iter():
        children = list(element)

        if len(children) < 2:
            continue

        # Find a useful human-readable name.
        tag = local_name(element.tag)

        element_id = element.get("id", "")
        if element_id:
            description = f"{tag}-id-{element_id}"
        else:
            description = tag

        midpoint = len(children) // 2

        for suffix, selected in [
            ("first-half", children[:midpoint]),
            ("second-half", children[midpoint:]),
        ]:
            new_root = copy.deepcopy(original_root)

            # Find the corresponding element in the copied tree.
            #
            # ElementTree doesn't provide parent pointers, so use
            # the path of child indexes from root to the original.
            path = element_path(original_root, element)

            copied_element = element_at_path(new_root, path)

            for child in list(copied_element):
                copied_element.remove(child)

            for child in selected:
                copied_element.append(copy.deepcopy(child))

            counter += 1

            name = (
                f"recursive-{counter:04d}-"
                f"{description}-{suffix}"
            )

            variants.append(
                (
                    name,
                    serialize_tree(ET.ElementTree(new_root)),
                )
            )

    return variants


def element_path(root: ET.Element, target: ET.Element) -> list[int]:
    """
    Return the child-index path from root to target.
    """

    path: list[int] = []

    def search(element: ET.Element) -> bool:
        if element is target:
            return True

        for index, child in enumerate(list(element)):
            path.append(index)

            if search(child):
                return True

            path.pop()

        return False

    if not search(root):
        raise RuntimeError("Could not locate XML element")

    return path


def element_at_path(
    root: ET.Element,
    path: list[int],
) -> ET.Element:
    """Follow a child-index path."""
    element = root

    for index in path:
        element = list(element)[index]

    return element


def make_raw_line_variants(
    original: bytes,
    count: int,
) -> list[tuple[str, bytes]]:
    """
    Fallback mode for malformed XHTML.

    This deliberately works on the raw file and therefore may produce
    malformed XHTML. It is useful only when the original XHTML cannot
    be parsed as XML.

    Each variant keeps the first N lines and then appends the original
    closing portion of the document when possible.
    """

    text = original.decode("utf-8", errors="replace")

    lines = text.splitlines(keepends=True)

    if not lines:
        return []

    variants = []

    for i in range(1, count + 1):
        n = max(1, round(len(lines) * i / count))

        partial = "".join(lines[:n])

        # Try to close the document in the simplest possible way.
        if "</body>" not in partial.lower():
            partial += "\n</body>"

        if "</html>" not in partial.lower():
            partial += "\n</html>\n"

        percentage = 100.0 * n / len(lines)

        variants.append(
            (
                f"raw-lines-{i:03d}-{percentage:06.2f}pct",
                partial.encode("utf-8"),
            )
        )

    return variants


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bisect an XHTML file inside an unpacked EPUB."
    )

    parser.add_argument(
        "epub_dir",
        type=Path,
        help="directory containing the unpacked EPUB",
    )

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("epub-bisect"),
        help="output directory (default: epub-bisect)",
    )

    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=16,
        help="number of cumulative prefix EPUBs (default: 16)",
    )

    parser.add_argument(
        "--recursive",
        action="store_true",
        help="also generate recursive half/half tests",
    )

    parser.add_argument(
        "--raw-lines",
        action="store_true",
        help="bisect raw lines instead of parsing XHTML",
    )

    args = parser.parse_args()

    epub_dir = args.epub_dir.resolve()
    target = epub_dir / TARGET
    output = args.output.resolve()

    if not epub_dir.is_dir():
        raise SystemExit(
            f"ERROR: not a directory: {epub_dir}"
        )

    if not target.exists():
        raise SystemExit(
            f"ERROR: target file does not exist:\n{target}"
        )

    if args.count < 2:
        raise SystemExit("--count must be at least 2")

    output.mkdir(parents=True, exist_ok=True)

    print(f"EPUB directory : {epub_dir}")
    print(f"Target         : {TARGET}")
    print(f"Output         : {output}")
    print()

    original = target.read_bytes()

    variants: list[tuple[str, bytes]] = []

    if args.raw_lines:
        print("Using RAW LINE mode.")
        print("WARNING: generated XHTML may be malformed.")
        variants.extend(
            make_raw_line_variants(
                original,
                args.count,
            )
        )
    else:
        print("Parsing XHTML...")

        tree = parse_xhtml(target)

        body = find_body(tree)

        print(
            f"Found <body> containing "
            f"{len(list(body))} direct child elements."
        )

        variants.extend(
            make_prefix_variants(
                tree,
                args.count,
            )
        )

        # Always include the simple half/half tests.
        variants.extend(
            make_half_variants(tree)
        )

        if args.recursive:
            print("Generating recursive variants...")
            variants.extend(
                make_recursive_variants(tree)
            )

    print(f"Generating {len(variants)} EPUB files...")
    print()

    manifest_rows = []

    for number, (name, xhtml_bytes) in enumerate(
        variants,
        start=1,
    ):
        filename = f"test-{number:04d}-{name}.epub"
        epub_path = output / filename

        write_epub(
            epub_dir,
            epub_path,
            xhtml_bytes,
        )

        manifest_rows.append(
            {
                "number": number,
                "file": filename,
                "variant": name,
                "size_bytes": len(xhtml_bytes),
            }
        )

        print(
            f"[{number:4d}/{len(variants)}] "
            f"{filename}"
        )

    manifest = output / "manifest.csv"

    with manifest.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "number",
                "file",
                "variant",
                "size_bytes",
            ],
        )

        writer.writeheader()
        writer.writerows(manifest_rows)

    print()
    print("Done.")
    print(f"Manifest: {manifest}")
    print()
    print(
        "Open the generated EPUBs in your problematic EPUB viewer "
        "and record which ones are slow."
    )


if __name__ == "__main__":
    main()
