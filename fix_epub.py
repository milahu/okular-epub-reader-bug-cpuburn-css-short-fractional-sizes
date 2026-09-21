#!/usr/bin/env python3
"""
Fix Kobo-generated EPUBs that cause excessive CPU usage in EPUB readers.

Usage:
    python fix_epub.py broken.epub

Output:
    broken.fixed.epub

The script:
  * removes Kobo JavaScript files/references
  * removes Kobo-specific <style> blocks containing koboSpanStyle
  * unwraps every <span class="koboSpan">...</span>
  * removes Kobo kobo.* IDs from those spans
  * removes Kobo-only -webkit-text-combine declarations
  * preserves the rest of the EPUB as byte-for-byte ZIP entries where possible
  * creates the output atomically
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


KOBO_JS_NAMES = {"kobo.js"}
XHTML_EXTENSIONS = {".xhtml", ".html", ".htm"}

# Match a Kobo span even if additional classes/attributes are present.
KOBO_SPAN_RE = re.compile(
    rb"<span\b(?=[^>]*\bclass\s*=\s*(['\"])[^'\"]*\bkoboSpan\b[^'\"]*\1)"
    rb"[^>]*>(.*?)</span\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Kobo IDs are of the form kobo.1.1, kobo.12.345, etc.
KOBO_ID_RE = re.compile(
    rb'\s+id\s*=\s*([\'"])kobo\.[^\'"]*\1',
    re.IGNORECASE,
)

# Remove the specific declaration regardless of whitespace.
TEXT_COMBINE_RE = re.compile(
    rb"\s*-webkit-text-combine\s*:\s*inherit\s*;?",
    re.IGNORECASE,
)

# Remove Kobo's dedicated style element. This is intentionally limited to
# styles whose id is koboSpanStyle, rather than deleting arbitrary CSS.
KOBO_STYLE_RE = re.compile(
    rb"<style\b(?=[^>]*\bid\s*=\s*(['\"])koboSpanStyle\1)[^>]*>.*?</style\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Remove references to kobo.js from HTML/XML documents.
KOBO_SCRIPT_TAG_RE = re.compile(
    rb"<script\b[^>]*\bsrc\s*=\s*(['\"])[^'\"]*kobo\.js[^'\"]*\1[^>]*>"
    rb".*?</script\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Also handle an unusual self-closing script element.
KOBO_SCRIPT_SELF_CLOSING_RE = re.compile(
    rb"<script\b[^>]*\bsrc\s*=\s*(['\"])[^'\"]*kobo\.js[^'\"]*\1[^>]*/\s*>",
    re.IGNORECASE | re.DOTALL,
)


def is_kobo_span_start(tag: bytes) -> bool:
    return (
        re.search(
            rb"<span\b(?=[^>]*\bclass\s*=\s*(['\"])[^'\"]*\bkoboSpan\b[^'\"]*\1)"
            rb"[^>]*>",
            tag,
            re.IGNORECASE,
        )
        is not None
    )


def unwrap_kobo_spans(data: bytes) -> tuple[bytes, int]:
    """
    Repeatedly unwrap koboSpan elements.

    A regex is deliberately used here instead of an XML parser because EPUB
    XHTML may contain entities/doctype constructs that we should not rewrite,
    normalize, or reserialize unnecessarily.
    """
    count = 0

    while True:
        match = KOBO_SPAN_RE.search(data)
        if not match:
            break

        whole = match.group(0)
        inner = match.group(2)

        # Only strip the Kobo ID from the wrapper itself. Preserve any other
        # attributes in the unlikely event that they exist.
        opening_end = whole.find(b">")
        opening = whole[: opening_end + 1]
        opening = KOBO_ID_RE.sub(b"", opening)

        # Since this is a koboSpan, discard its wrapper attributes entirely.
        # Kobo spans are present solely as text/pagination markers in this EPUB.
        # Keep only their content.
        data = data[: match.start()] + inner + data[match.end() :]
        count += 1

    return data, count


def clean_xhtml(data: bytes) -> tuple[bytes, dict[str, int]]:
    stats = {
        "kobo_spans": 0,
        "kobo_styles": 0,
        "text_combine": 0,
        "kobo_scripts": 0,
    }

    data, stats["kobo_styles"] = KOBO_STYLE_RE.subn(b"", data)
    data, stats["text_combine"] = TEXT_COMBINE_RE.subn(b"", data)

    data, n = KOBO_SCRIPT_TAG_RE.subn(b"", data)
    stats["kobo_scripts"] += n
    data, n = KOBO_SCRIPT_SELF_CLOSING_RE.subn(b"", data)
    stats["kobo_scripts"] += n

    data, stats["kobo_spans"] = unwrap_kobo_spans(data)

    return data, stats


def default_output_path(input_path: Path) -> Path:
    if input_path.suffix.lower() == ".epub":
        return input_path.with_suffix(".fixed.epub")
    return input_path.with_name(input_path.name + ".fixed.epub")


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {Path(sys.argv[0]).name} INPUT.epub", file=sys.stderr)
        return 2

    input_path = Path(sys.argv[1]).expanduser().resolve()

    if not input_path.is_file():
        print(f"Error: file not found: {input_path}", file=sys.stderr)
        return 1

    if not zipfile.is_zipfile(input_path):
        print(f"Error: not a valid ZIP/EPUB file: {input_path}", file=sys.stderr)
        return 1

    output_path = default_output_path(input_path)
    tmp_path = output_path.with_name(output_path.name + ".tmp")

    total = {
        "files": 0,
        "changed": 0,
        "kobo_spans": 0,
        "kobo_styles": 0,
        "text_combine": 0,
        "kobo_scripts": 0,
        "kobo_js_removed": 0,
    }

    try:
        with zipfile.ZipFile(input_path, "r") as zin:
            bad = zin.testzip()
            if bad is not None:
                raise RuntimeError(f"corrupt ZIP member: {bad}")

            with zipfile.ZipFile(
                tmp_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as zout:
                for info in zin.infolist():
                    name = info.filename
                    total["files"] += 1

                    # Never copy a Kobo JS payload into the fixed EPUB.
                    if Path(name).name.lower() in KOBO_JS_NAMES:
                        total["kobo_js_removed"] += 1
                        continue

                    data = zin.read(info)

                    if Path(name).suffix.lower() in XHTML_EXTENSIONS:
                        new_data, stats = clean_xhtml(data)
                        if new_data != data:
                            total["changed"] += 1
                        for key in (
                            "kobo_spans",
                            "kobo_styles",
                            "text_combine",
                            "kobo_scripts",
                        ):
                            total[key] += stats[key]
                        data = new_data

                    # Preserve ZIP metadata as much as Python's zipfile allows.
                    new_info = zipfile.ZipInfo(filename=info.filename)
                    new_info.date_time = info.date_time
                    new_info.comment = info.comment
                    new_info.extra = info.extra
                    new_info.create_system = info.create_system
                    new_info.create_version = info.create_version
                    new_info.extract_version = info.extract_version
                    new_info.flag_bits = info.flag_bits
                    new_info.external_attr = info.external_attr
                    new_info.internal_attr = info.internal_attr

                    # EPUB readers care about the mimetype entry being stored
                    # uncompressed. Preserve that convention.
                    if name == "mimetype":
                        zout.writestr(
                            new_info,
                            data,
                            compress_type=zipfile.ZIP_STORED,
                        )
                    else:
                        zout.writestr(
                            new_info,
                            data,
                            compress_type=zipfile.ZIP_DEFLATED,
                        )

        tmp_path.replace(output_path)

    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    print(f"Input : {input_path}")
    print(f"Output: {output_path}")
    print()
    print(f"XHTML files changed       : {total['changed']}")
    print(f"koboSpan wrappers removed : {total['kobo_spans']}")
    print(f"koboSpanStyle blocks removed: {total['kobo_styles']}")
    print(f"-webkit-text-combine removed: {total['text_combine']}")
    print(f"kobo.js script references : {total['kobo_scripts']}")
    print(f"kobo.js files removed     : {total['kobo_js_removed']}")
    print()
    print("Done.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
