#!/usr/bin/env python3

import sys

from tree_sitter import Language, Parser
import tree_sitter_html


def make_parser():
    language = Language(tree_sitter_html.language())

    try:
        return Parser(language)
    except TypeError:
        parser = Parser()
        parser.set_language(language)
        return parser


def dump(node, data, depth=0, max_depth=8):
    indent = "  " * depth

    text = data[node.start_byte:node.end_byte]

    # Keep the dump readable.
    if len(text) > 160:
        text = text[:160] + b"..."

    text = (
        text
        .replace(b"\n", b"\\n")
        .replace(b"\r", b"\\r")
        .replace(b"\t", b"\\t")
    )

    print(
        f"{indent}{node.type:30s} "
        f"{node.start_byte:8d}-{node.end_byte:<8d} "
        f"{text!r}"
    )

    if depth >= max_depth:
        return

    for child in node.children:
        dump(child, data, depth + 1, max_depth)


def main():
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} FILE.xhtml")
        sys.exit(2)

    path = sys.argv[1]

    with open(path, "rb") as f:
        data = f.read()

    print("=" * 80)
    print("TREE-SITTER DEBUG")
    print("=" * 80)
    print(f"File: {path}")
    print(f"Bytes: {len(data)}")
    print()

    parser = make_parser()
    tree = parser.parse(data)

    print("ROOT:")
    print("-" * 80)
    dump(tree.root_node, data)

    print()
    print("=" * 80)
    print("NODES CONTAINING 'koboSpan'")
    print("=" * 80)

    found = 0

    def walk(node):
        nonlocal found

        text = data[node.start_byte:node.end_byte]

        if b"koboSpan" in text:
            found += 1

            print()
            print(
                f"NODE #{found}: "
                f"{node.type} "
                f"{node.start_byte}-{node.end_byte}"
            )
            print(repr(text[:500]))

            print("\nCST:")
            dump(node, data, depth=0, max_depth=8)

        for child in node.children:
            walk(child)

    walk(tree.root_node)

    print()
    print("=" * 80)
    print(f"Nodes containing koboSpan: {found}")
    print("=" * 80)


if __name__ == "__main__":
    main()
