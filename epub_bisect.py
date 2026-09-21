#!/usr/bin/env python3

import argparse
import posixpath
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET


CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx"

ET.register_namespace("", OPF_NS)
ET.register_namespace("dc", DC_NS)


def qname(namespace, tag):
    return f"{{{namespace}}}{tag}"


def localname(tag):
    """Return the local part of an XML tag."""
    return tag.rsplit("}", 1)[-1]


def safe_filename(name):
    """Make a string safe for use as a filename."""
    name = re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE)
    return name.strip("._") or "document"


def find_opf(zf):
    """Find the OPF file from META-INF/container.xml."""
    data = zf.read("META-INF/container.xml")
    root = ET.fromstring(data)

    for elem in root.iter():
        if localname(elem.tag) == "rootfile":
            full_path = elem.attrib.get("full-path")
            if full_path:
                return full_path

    raise RuntimeError("Could not find OPF file in container.xml")


def read_xml(zf, filename):
    return ET.fromstring(zf.read(filename))


def get_manifest_and_spine(opf_root):
    manifest = {}
    spine = []

    for elem in opf_root:
        if localname(elem.tag) == "manifest":
            for item in elem:
                if localname(item.tag) == "item":
                    manifest[item.attrib["id"]] = dict(item.attrib)

        elif localname(elem.tag) == "spine":
            for itemref in elem:
                if localname(itemref.tag) == "itemref":
                    spine.append(itemref.attrib["idref"])

    return manifest, spine


def is_html_item(item):
    media_type = item.get("media-type", "").lower()
    href = item.get("href", "").lower()

    return (
        media_type in (
            "application/xhtml+xml",
            "text/html",
            "application/html",
        )
        or href.endswith((".xhtml", ".html", ".htm"))
    )


def href_to_path(opf_path, href):
    """
    Convert an OPF-relative href to the archive path.

    Handles simple URL fragments and percent-encoded paths reasonably.
    """
    href = href.split("#", 1)[0]
    href = href.split("?", 1)[0]

    opf_dir = posixpath.dirname(opf_path)
    path = posixpath.normpath(posixpath.join(opf_dir, href))

    # EPUB paths use forward slashes regardless of OS.
    return path


def path_to_href(from_file, to_file):
    """Create a relative EPUB href from one archive path to another."""
    from_dir = posixpath.dirname(from_file)
    return posixpath.relpath(to_file, from_dir)


def remove_html_manifest_items(opf_root, keep_id):
    """
    Remove all HTML/XHTML manifest entries except the selected one.
    Also remove their corresponding spine itemrefs.
    """
    removed_ids = set()

    for parent in list(opf_root):
        if localname(parent.tag) == "manifest":
            for item in list(parent):
                if localname(item.tag) != "item":
                    continue

                item_id = item.attrib.get("id")
                media_type = item.attrib.get("media-type", "").lower()
                href = item.attrib.get("href", "").lower()

                html = (
                    media_type in (
                        "application/xhtml+xml",
                        "text/html",
                        "application/html",
                    )
                    or href.endswith((".xhtml", ".html", ".htm"))
                )

                if html and item_id != keep_id:
                    removed_ids.add(item_id)
                    parent.remove(item)

    for parent in list(opf_root):
        if localname(parent.tag) == "spine":
            for itemref in list(parent):
                if (
                    localname(itemref.tag) == "itemref"
                    and itemref.attrib.get("idref") in removed_ids
                ):
                    parent.remove(itemref)

    return removed_ids


def simplify_spine(opf_root, keep_id):
    """
    Make the spine contain only the selected content document.
    """
    for parent in opf_root:
        if localname(parent.tag) == "spine":
            for itemref in list(parent):
                if localname(itemref.tag) != "itemref":
                    continue

                if itemref.attrib.get("idref") != keep_id:
                    parent.remove(itemref)

            break


def update_ncx(ncx_data, target_href):
    """
    Produce an NCX containing only a single navPoint.

    This is intentionally simple. NCX is XML, not HTML, so the resulting
    EPUB still has only one HTML/XHTML document.
    """
    try:
        root = ET.fromstring(ncx_data)
    except ET.ParseError:
        return ncx_data

    # Remove all navPoint elements.
    navmap = None
    for elem in root.iter():
        if localname(elem.tag) == "navMap":
            navmap = elem
            break

    if navmap is not None:
        for child in list(navmap):
            if localname(child.tag) == "navPoint":
                navmap.remove(child)

        navpoint = ET.Element(qname(NCX_NS, "navPoint"), {
            "id": "navPoint-1",
            "playOrder": "1",
        })

        label = ET.SubElement(navpoint, qname(NCX_NS, "navLabel"))
        text = ET.SubElement(label, qname(NCX_NS, "text"))
        text.text = "Document"

        content = ET.SubElement(navpoint, qname(NCX_NS, "content"))
        content.set("src", target_href)

        navmap.append(navpoint)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def find_ncx_path(opf_path, manifest):
    for item in manifest.values():
        media_type = item.get("media-type", "").lower()

        if media_type == "application/x-dtbncx+xml":
            return href_to_path(opf_path, item["href"])

    return None


def write_epub(output_path, original_zip, files_to_copy, modified_files):
    """
    Write a valid EPUB ZIP archive.

    mimetype must be the first file and must be stored uncompressed.
    """
    with zipfile.ZipFile(output_path, "w") as out:
        # EPUB requirement: mimetype must be first and uncompressed.
        out.writestr(
            "mimetype",
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )

        for archive_path in files_to_copy:
            if archive_path == "mimetype":
                continue

            if archive_path in modified_files:
                data = modified_files[archive_path]
            else:
                data = original_zip.read(archive_path)

            out.writestr(
                archive_path,
                data,
                compress_type=zipfile.ZIP_DEFLATED,
            )


def split_epub(input_epub, output_dir):
    input_epub = Path(input_epub)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(input_epub, "r") as zf:
        names = zf.namelist()

        if "META-INF/container.xml" not in names:
            raise RuntimeError("Not a valid EPUB: container.xml is missing")

        opf_path = find_opf(zf)
        opf_root = read_xml(zf, opf_path)

        manifest, spine = get_manifest_and_spine(opf_root)

        # Get actual HTML/XHTML documents from the spine.
        spine_html = []

        for item_id in spine:
            item = manifest.get(item_id)

            if item and is_html_item(item):
                archive_path = href_to_path(opf_path, item["href"])

                if archive_path in names:
                    spine_html.append(
                        (item_id, item, archive_path)
                    )

        if not spine_html:
            raise RuntimeError(
                "No HTML/XHTML content documents were found in the EPUB spine."
            )

        print(f"Found {len(spine_html)} HTML/XHTML documents.")

        # Preserve everything except the other HTML/XHTML documents.
        all_html_paths = set()

        for item in manifest.values():
            if is_html_item(item):
                all_html_paths.add(href_to_path(opf_path, item["href"]))

        # Locate NCX, if present.
        ncx_path = find_ncx_path(opf_path, manifest)

        for index, (keep_id, keep_item, keep_path) in enumerate(spine_html, 1):

            title = Path(keep_path).stem
            output_name = (
                f"{index:03d}_{safe_filename(title)}.epub"
            )
            output_path = output_dir / output_name

            # Start from a fresh copy of the OPF.
            new_opf_root = read_xml(zf, opf_path)

            # Remove every HTML/XHTML manifest item except the selected one.
            remove_html_manifest_items(new_opf_root, keep_id)

            # Make the spine contain only the selected document.
            simplify_spine(new_opf_root, keep_id)

            modified_files = {
                opf_path: ET.tostring(
                    new_opf_root,
                    encoding="utf-8",
                    xml_declaration=True,
                )
            }

            # If there is an NCX, simplify it too.
            if ncx_path and ncx_path in names:
                try:
                    # NCX content paths are relative to the NCX file.
                    target_href = path_to_href(ncx_path, keep_path)
                    modified_files[ncx_path] = update_ncx(
                        zf.read(ncx_path),
                        target_href,
                    )
                except Exception as exc:
                    print(
                        f"Warning: could not simplify NCX: {exc}"
                    )

            # Copy every original file except the other HTML/XHTML files.
            files_to_copy = []

            for archive_path in names:
                if archive_path in all_html_paths:
                    if archive_path != keep_path:
                        continue

                files_to_copy.append(archive_path)

            write_epub(
                output_path,
                zf,
                files_to_copy,
                modified_files,
            )

            print(
                f"[{index:3d}/{len(spine_html)}] "
                f"{keep_path} -> {output_path.name}"
            )

    print()
    print(f"Created {len(spine_html)} EPUB files in:")
    print(f"  {output_dir}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Split an EPUB into one EPUB per HTML/XHTML spine document."
        )
    )

    parser.add_argument(
        "input",
        help="Input EPUB file",
    )

    parser.add_argument(
        "-o",
        "--output",
        default="epub_split",
        help="Output directory (default: epub_split)",
    )

    args = parser.parse_args()

    split_epub(args.input, args.output)


if __name__ == "__main__":
    main()
