#!/usr/bin/env python3

import argparse
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
import random

DEFAULT_TIMEOUT = 5.0
DEFAULT_INTERVAL = 0.05
DEFAULT_IDLE_THRESHOLD = 5.0
DEFAULT_IDLE_SAMPLES = 3


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkResult:
    good: bool
    elapsed: float
    peak_cpu: float


def process_snapshot():
    import psutil

    return {
        p.pid
        for p in psutil.process_iter(
            ["pid", "name", "cmdline"]
        )
    }


def find_new_reader_process(before, epub_reader):
    import psutil

    reader_name = Path(epub_reader).name

    for proc in psutil.process_iter(
        ["pid", "name", "cmdline"]
    ):
        try:
            if proc.pid in before:
                continue

            name = proc.info["name"] or ""
            cmdline = proc.info["cmdline"] or []

            if name == reader_name:
                return proc.pid

            if any(
                Path(arg).name == reader_name
                for arg in cmdline
                if arg
            ):
                return proc.pid

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
        ):
            continue

    return None


def kill_pid(pid):
    import psutil

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return

    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except (
        psutil.TimeoutExpired,
        psutil.NoSuchProcess,
    ):
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass


def benchmark_epub(
        epub_path,
        epub_reader,
        timeout,
        interval,
        idle_threshold,
        idle_samples,
    ):
    import psutil

    before = process_snapshot()
    started = time.monotonic()

    launcher = subprocess.Popen(
        [epub_reader, str(epub_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    target_pid = find_new_reader_process(
        before,
        epub_reader,
    )

    if target_pid is None:
        target_pid = launcher.pid

    proc = psutil.Process(target_pid)
    proc.cpu_percent(None)

    peak_cpu = 0.0
    idle_count = 0
    good = False

    deadline = started + timeout

    while time.monotonic() < deadline:
        time.sleep(interval)

        if not proc.is_running():
            good = True
            break

        cpu = proc.cpu_percent(None)
        peak_cpu = max(peak_cpu, cpu)

        if cpu <= idle_threshold:
            idle_count += 1

            if idle_count >= idle_samples:
                good = True
                break
        else:
            idle_count = 0

    elapsed = time.monotonic() - started

    if target_pid != launcher.pid:
        kill_pid(target_pid)

    kill_pid(launcher.pid)

    return BenchmarkResult(
        good=good,
        elapsed=elapsed,
        peak_cpu=peak_cpu,
    )


# ---------------------------------------------------------------------------
# EPUB packing
# ---------------------------------------------------------------------------

def pack_epub(directory, output_epub):
    output_epub.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with zipfile.ZipFile(
        output_epub,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as z:
        mimetype = directory / "mimetype"

        if mimetype.exists():
            z.write(
                mimetype,
                "mimetype",
                compress_type=zipfile.ZIP_STORED,
            )

        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue

            rel = path.relative_to(directory)

            if rel.as_posix() == "mimetype":
                continue

            if rel.suffix == ".epub":
                continue

            # Same behavior as the main minimizer.
            if rel.suffix in (".jpg", ".otf"):
                continue

            z.write(
                path,
                rel.as_posix(),
                compress_type=zipfile.ZIP_STORED,
            )


# ---------------------------------------------------------------------------
# Tree-sitter XML
# ---------------------------------------------------------------------------

def make_xml_parser():
    import tree_sitter_xml
    from tree_sitter import Language, Parser

    if hasattr(tree_sitter_xml, "language_xml"):
        language = Language(
            tree_sitter_xml.language_xml()
        )
    elif hasattr(tree_sitter_xml, "language"):
        language = Language(
            tree_sitter_xml.language()
        )
    else:
        raise RuntimeError(
            "tree_sitter_xml has neither "
            "language_xml() nor language()"
        )

    try:
        return Parser(language)
    except TypeError:
        parser = Parser()

        try:
            parser.language = language
        except AttributeError:
            parser.set_language(language)

        return parser


def direct_child(node, node_type):
    for child in node.children:
        if child.type == node_type:
            return child

    return None


def xml_element_name(data, element):
    stag = direct_child(element, "STag")

    if stag is None:
        return None

    name = direct_child(stag, "Name")

    if name is None:
        return None

    return data[
        name.start_byte:name.end_byte
    ].decode(
        "utf-8",
        errors="replace",
    )


def element_attributes(data, element):
    """
    Return attributes from an XML element.

    Result:
        {
            "class": "Register",
            "id": "...",
            ...
        }
    """
    stag = direct_child(element, "STag")

    if stag is None:
        return {}

    result = {}

    for child in stag.children:
        if child.type != "Attribute":
            continue

        name_node = direct_child(
            child,
            "Name",
        )

        if name_node is None:
            continue

        name = data[
            name_node.start_byte:name_node.end_byte
        ].decode(
            "utf-8",
            errors="replace",
        )

        value = None

        for value_node in child.children:
            if value_node.type in (
                "AttValue",
                "QuotedAttValue",
            ):
                value = data[
                    value_node.start_byte:value_node.end_byte
                ].decode(
                    "utf-8",
                    errors="replace",
                )

                if (
                    len(value) >= 2
                    and value[0] in "\"'"
                    and value[-1] == value[0]
                ):
                    value = value[1:-1]

                break

        result[name] = value

    return result


@dataclass
class XmlElement:
    node: object
    name: str
    start: int
    end: int
    attributes: dict
    parent: object | None = None
    children: list = None

    def __post_init__(self):
        if self.children is None:
            self.children = []


def build_xml_tree(data):
    parser = make_xml_parser()
    tree = parser.parse(data)

    root = tree.root_node

    html_node = None

    for child in root.children:
        if child.type != "element":
            continue

        name = xml_element_name(data, child)

        if name == "html":
            html_node = child
            break

    if html_node is None:
        raise RuntimeError(
            "Could not find root <html> element"
        )

    def build(node, parent=None):
        if node.type != "element":
            return None

        name = xml_element_name(data, node)

        if name is None:
            return None

        element = XmlElement(
            node=node,
            name=name,
            start=node.start_byte,
            end=node.end_byte,
            attributes=element_attributes(
                data,
                node,
            ),
            parent=parent,
        )

        for child in node.children:
            if child.type != "content":
                continue

            for content_child in child.children:
                if content_child.type != "element":
                    continue

                child_element = build(
                    content_child,
                    element,
                )

                if child_element is not None:
                    element.children.append(
                        child_element
                    )

        return element

    return build(html_node)


def flatten_tree(root):
    result = []

    def walk(node):
        result.append(node)

        for child in node.children:
            walk(child)

    walk(root)

    return result


# ---------------------------------------------------------------------------
# Byte-preserving removal
# ---------------------------------------------------------------------------

def remove_ranges(data, ranges):
    ranges = sorted(ranges)

    output = []
    pos = 0

    for start, end in ranges:
        output.append(data[pos:start])
        pos = end

    output.append(data[pos:])

    return b"".join(output)


def remove_elements(data, elements):
    return remove_ranges(
        data,
        [
            (element.start, element.end)
            for element in elements
        ],
    )


# ---------------------------------------------------------------------------
# Register discovery
# ---------------------------------------------------------------------------

def find_register_paragraphs(root):
    result = []

    for element in flatten_tree(root):
        if element.name != "p":
            continue

        if element.attributes.get("class") != "Register":
            continue

        result.append(element)

    return sorted(
        result,
        key=lambda element: element.start,
    )


def find_register_title(root):
    """
    Find the <div class="appendix-title-group"> associated with
    the Register.

    This deliberately uses the structural class rather than relying
    on byte offsets.
    """
    for element in flatten_tree(root):
        if element.name != "div":
            continue

        if (
            element.attributes.get("class")
            == "appendix-title-group"
        ):
            return element

    return None


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------

def logarithmic_counts(n, logarithmic_base=2):
    """
    Return:

        1, 2, 4, 8, ... <= n

    plus n itself.

    Avoid duplicates.
    """
    result = []

    count = 1

    while count < n:
        result.append(count)
        count *= logarithmic_base

    result.append(n)

    return sorted(set(result))


def decreasing_removal_counts(n, logarithmic_base=2):
    """
    Return removal counts:

        n-1, n-2, n-4, n-8, ...

    Equivalently, this keeps:

        1, 2, 4, 8, ...

    Register paragraphs.

    The final count is 0 if necessary, meaning "remove none".
    """
    result = []

    remaining = 1

    while remaining < n:
        result.append(n - remaining)
        remaining *= logarithmic_base

    # If n itself was not reached by the doubling sequence,
    # also test removing zero paragraphs.
    if result[-1] != 0:
        result.append(0)

    return result


def run_decreasing_removal_tests_zzzzz(
        register_paragraphs,
        run_test,
        label_prefix="remove first",
        max_consecutive_bad=2,
    ):
    """
    Start with almost all Register paragraphs removed and
    progressively restore paragraphs.

    Stop after `max_consecutive_bad` consecutive BAD results.
    """

    n = len(register_paragraphs)

    consecutive_bad = 0
    results = []

    remaining = 1

    while remaining <= n:
        remove_count = n - remaining

        result = run_test(
            (
                f"{label_prefix} {remove_count} "
                f"Register paragraph(s) "
                f"(leaving {remaining})"
            ),
            register_paragraphs[:remove_count],
        )

        results.append(
            (remove_count, remaining, result)
        )

        if result.good:
            consecutive_bad = 0
        else:
            consecutive_bad += 1

            if consecutive_bad >= max_consecutive_bad:
                print(
                    f"[STOP] "
                    f"{consecutive_bad} consecutive BAD results."
                )
                break

        remaining *= 2

    return results


def run_decreasing_removal_tests(
        register_paragraphs,
        run_test,
        extra_removed=None,
        label_prefix="remove first",
        max_consecutive_bad=2,
    ):
    """
    Start with almost all Register paragraphs removed and
    progressively restore paragraphs.

    `extra_removed` is a list of elements that are always removed.
    """

    if extra_removed is None:
        extra_removed = []

    n = len(register_paragraphs)

    consecutive_bad = 0
    results = []

    remaining = 1

    while remaining <= n:
        remove_count = n - remaining

        removed = (
            list(extra_removed)
            + register_paragraphs[:remove_count]
        )

        result = run_test(
            (
                f"{label_prefix} {remove_count} "
                f"Register paragraph(s) "
                f"(leaving {remaining})"
            ),
            removed,
        )

        results.append(
            (remove_count, remaining, result)
        )

        if result.good:
            consecutive_bad = 0
        else:
            consecutive_bad += 1

            if consecutive_bad >= max_consecutive_bad:
                print(
                    f"[STOP] "
                    f"{consecutive_bad} consecutive BAD results."
                )
                break

        remaining *= 2

    return results


def describe_result(label, result):
    status = "GOOD" if result.good else "BAD"

    print(
        f"[{status}] {label:<55} "
        f"{result.elapsed:6.2f}s "
        f"peak={result.peak_cpu:6.1f}%"
    )


# refine search 1
# find the exact number of paragraphs we have to remove
# to turn a good epub file into a bad epub file

def refine_register_threshold(
        register_paragraphs,
        run_test,
        good_leaving,
        bad_leaving,
    ):
    """
    Binary-search the transition between a known GOOD and BAD count.
    """

    print()
    print("=" * 70)
    print("REFINING REGISTER PARAGRAPH THRESHOLD")
    print("=" * 70)

    while bad_leaving - good_leaving > 1:
        leaving = (good_leaving + bad_leaving) // 2
        remove_count = len(register_paragraphs) - leaving

        result = run_test(
            f"binary search: leaving {leaving} Register paragraph(s)",
            register_paragraphs[:remove_count],
        )

        if result.good:
            good_leaving = leaving
        else:
            bad_leaving = leaving

        print(
            f"    bracket: "
            f"GOOD={good_leaving}, "
            f"BAD={bad_leaving}"
        )

    print()
    print(
        f"[RESULT] threshold between "
        f"{good_leaving} and {bad_leaving} Register paragraphs"
    )

    return good_leaving, bad_leaving


def run_restore_register_tests(
        register_paragraphs,
        run_test,
        max_consecutive_bad=2,
    ):
    """
    Test 2: restore Register paragraphs in exponentially increasing amounts.

    We test:
        leaving 1, 2, 4, 8, 16, 32, ...

    and stop after max_consecutive_bad BAD results.

    Returns:
        (good_leaving, bad_leaving)

    where both values are discovered from the actual tests.
    """

    print()
    print("=" * 70)
    print("TEST 2: RESTORE REGISTER PARAGRAPHS")
    print("=" * 70)

    n = len(register_paragraphs)

    last_good = None
    first_bad = None
    consecutive_bad = 0

    leaving = 1

    while leaving <= n:
        remove_count = n - leaving

        result = run_test(
            f"remove first {remove_count} Register paragraph(s) "
            f"(leaving {leaving})",
            register_paragraphs[:remove_count],
        )

        if result.good:
            last_good = leaving
            consecutive_bad = 0
        else:
            # The first BAD after a GOOD gives us a useful bracket.
            if first_bad is None and last_good is not None:
                first_bad = leaving

            consecutive_bad += 1

            if consecutive_bad >= max_consecutive_bad:
                break

        leaving *= 2

    if last_good is None or first_bad is None:
        print()
        print("[WARN] Test 2 did not find a GOOD/BAD bracket.")
        return None, None

    print()
    print(
        f"[BRACKET] GOOD at {last_good} Register paragraphs"
    )
    print(
        f"[BRACKET] BAD at {first_bad} Register paragraphs"
    )

    return last_good, first_bad


# refine search 2
# test the order of removed paragraphs
# is there a difference between
# - "remove the last 248 paragraphs"
# - "remove the first 248 paragraphs"
# - "remove 248 random paragraphs"

def select_register_paragraphs(
        register_paragraphs,
        count,
        mode,
        seed=12345,
    ):
    """
    Select exactly `count` Register paragraphs.

    mode:
        "first"  - first `count`
        "last"   - last `count`
        "random" - random subset, preserving original document order
    """

    if count > len(register_paragraphs):
        raise ValueError(
            f"Cannot select {count} paragraphs from "
            f"{len(register_paragraphs)}"
        )

    if mode == "first":
        return register_paragraphs[:count]

    if mode == "last":
        return register_paragraphs[-count:]

    if mode == "random":
        rng = random.Random(seed)
        selected = rng.sample(register_paragraphs, count)

        # Keep document order after selecting randomly.
        return sorted(selected, key=lambda element: element.start)

    raise ValueError(f"Unknown selection mode: {mode}")


def test_register_selection_growth(
        register_paragraphs,
        run_test,
        good_leaving,
        random_seed=12345,
        max_consecutive_bad=2,
    ):
    """
    Starting from a known GOOD paragraph count, progressively add
    Register paragraphs:

        good_leaving
        good_leaving + 1
        good_leaving + 2
        good_leaving + 4
        good_leaving + 8
        ...

    Test first, last, and random selections independently.

    Stops each mode after max_consecutive_bad consecutive BAD results.
    """

    print()
    print("=" * 70)
    print("TEST: GROW REGISTER PARAGRAPH SET")
    print("=" * 70)

    n = len(register_paragraphs)

    modes = ("first", "last", "random")

    for mode in modes:
        print()
        print("-" * 70)
        print(f"MODE: {mode}")
        print("-" * 70)

        consecutive_bad = 0

        # Always begin with the known GOOD case.
        counts = [good_leaving]

        increment = 1
        while counts[-1] < n:
            next_count = good_leaving + increment

            if next_count > n:
                next_count = n

            if next_count != counts[-1]:
                counts.append(next_count)

            increment *= 2

        for count in counts:
            selected = select_register_paragraphs(
                register_paragraphs,
                count,
                mode,
                seed=random_seed,
            )

            selected_set = {
                (element.start, element.end)
                for element in selected
            }

            removed = [
                element
                for element in register_paragraphs
                if (element.start, element.end) not in selected_set
            ]

            result = run_test(
                f"keep {count} Register paragraphs ({mode})",
                removed,
            )

            if result.good:
                consecutive_bad = 0
            else:
                consecutive_bad += 1

                if consecutive_bad >= max_consecutive_bad:
                    print(
                        f"[STOP] {mode}: "
                        f"{consecutive_bad} consecutive BAD results."
                    )
                    break


# ---------------------------------------------------------------------------
# Main diagnostic
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Investigate whether <p class=\"Register\"> "
            "elements are responsible for an Okular CPU problem."
        )
    )

    parser.add_argument(
        "epub",
        type=Path,
        help="Original pathological EPUB",
    )

    parser.add_argument(
        "--xhtml",
        type=Path,
        required=True,
        help=(
            "Path to the XHTML file inside the unpacked EPUB "
            "directory."
        ),
    )

    parser.add_argument(
        "--reader",
        required=True,
        help="EPUB reader executable, e.g. okular",
    )

    parser.add_argument(
        "--epub-root",
        type=Path,
        required=True,
        help=(
            "Directory containing the unpacked EPUB. "
            "The XHTML path is interpreted relative to this directory "
            "if it is relative."
        ),
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
    )

    parser.add_argument(
        "--idle-threshold",
        type=float,
        default=DEFAULT_IDLE_THRESHOLD,
    )

    parser.add_argument(
        "--idle-samples",
        type=int,
        default=DEFAULT_IDLE_SAMPLES,
    )

    args = parser.parse_args()

    epub_root = args.epub_root.resolve()

    html_path = args.xhtml

    if not html_path.is_absolute():
        html_path = (
            epub_root / html_path
        ).resolve()
    else:
        html_path = html_path.resolve()

    if not args.epub.exists():
        raise RuntimeError(
            f"EPUB does not exist: {args.epub}"
        )

    if not html_path.exists():
        raise RuntimeError(
            f"XHTML does not exist: {html_path}"
        )

    try:
        html_relpath = html_path.relative_to(
            epub_root
        )
    except ValueError:
        raise RuntimeError(
            f"XHTML {html_path} is not inside "
            f"EPUB root {epub_root}"
        )

    original = html_path.read_bytes()

    print(
        f"[INFO] XHTML: {html_path}"
    )
    print(
        f"[INFO] XHTML size: {len(original):,} bytes"
    )

    root = build_xml_tree(original)

    register_paragraphs = find_register_paragraphs(
        root
    )

    register_title = find_register_title(root)

    print(
        f"[INFO] Register paragraphs: "
        f"{len(register_paragraphs)}"
    )

    if register_title is None:
        print(
            "[WARN] Could not find "
            '<div class="appendix-title-group">'
        )
    else:
        print(
            f"[INFO] Register title: "
            f"{register_title.start}:"
            f"{register_title.end}"
        )

    if not register_paragraphs:
        raise RuntimeError(
            'No <p class="Register"> elements found.'
        )

    # ------------------------------------------------------------
    # Candidate EPUB creation
    # ------------------------------------------------------------

    def test_xhtml(candidate_xhtml):
        with tempfile.TemporaryDirectory(
            prefix="register-test-"
        ) as tmp:
            tmpdir = Path(tmp)

            shutil.copytree(
                epub_root,
                tmpdir,
                dirs_exist_ok=True,
            )

            candidate_file = (
                tmpdir / html_relpath
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
                    args.reader,
                    args.timeout,
                    args.interval,
                    args.idle_threshold,
                    args.idle_samples,
                )
            finally:
                try:
                    candidate_epub.unlink()
                except FileNotFoundError:
                    pass

    def run_test(label, removed):
        candidate = remove_elements(
            original,
            removed,
        )

        result = test_xhtml(candidate)

        describe_result(
            label,
            result,
        )

        return result

    # ------------------------------------------------------------
    # Baseline
    # ------------------------------------------------------------

    print()
    print("=" * 70)
    print("BASELINE")
    print("=" * 70)

    baseline = test_xhtml(original)

    describe_result(
        "original",
        baseline,
    )

    if baseline.good:
        raise RuntimeError(
            "Original EPUB is GOOD; expected BAD."
        )

    # ------------------------------------------------------------
    # Test 1: title only
    # ------------------------------------------------------------

    print()
    print("=" * 70)
    print("TEST 1: REGISTER TITLE ONLY")
    print("=" * 70)

    if register_title is not None:
        run_test(
            "remove Register title <div>",
            [register_title],
        )

    # ------------------------------------------------------------
    # Tests 2 + 3:
    #
    # Remove first 1, 2, 4, 8, ... Register paragraphs.
    #
    # Then remove ALL Register paragraphs.
    # ------------------------------------------------------------

    # logarithmic_base = 2 # too slow
    logarithmic_base = 10

    paragraph_to_remove_counts = logarithmic_counts(
        len(register_paragraphs),
        logarithmic_base,
    )

    # print()
    # print("=" * 70)
    # print(
    #     "TEST 2: REMOVE FIRST N REGISTER PARAGRAPHS"
    # )
    # print("=" * 70)

    # for count in paragraph_to_remove_counts:
    #     run_test(
    #         f"remove first {count} Register paragraph(s)",
    #         register_paragraphs[:count],
    #     )

    # print()
    # print("=" * 70)
    # print("TEST 2: RESTORE REGISTER PARAGRAPHS")
    # print("=" * 70)

    # run_decreasing_removal_tests(
    #     register_paragraphs,
    #     run_test,
    # )

    good_leaving, bad_leaving = run_restore_register_tests(
        register_paragraphs,
        run_test,
    )

    if good_leaving is not None and bad_leaving is not None:

        # refine search 1

        good_leaving, bad_leaving = refine_register_threshold(
            register_paragraphs,
            run_test,
            good_leaving,
            bad_leaving,
        )

        # refine search 2

        test_register_selection_growth(
            register_paragraphs,
            run_test,
            good_leaving=good_leaving,
        )

    return # ignore other tests

    # ------------------------------------------------------------
    # Test 4:
    #
    # Remove all Register paragraphs EXCEPT ONE.
    #
    # This is especially interesting:
    #
    #   if GOOD -> the problem requires at least two Register
    #              paragraphs / their interaction
    #
    #   if BAD  -> even one remaining Register paragraph is enough
    #              to reproduce the problem.
    # ------------------------------------------------------------

    print()
    print("=" * 70)
    print(
        "TEST 3: REMOVE ALL EXCEPT ONE REGISTER PARAGRAPH"
    )
    print("=" * 70)

    for keep_index in (
        0,
        len(register_paragraphs) // 2,
        len(register_paragraphs) - 1,
    ):
        keep = register_paragraphs[keep_index]

        removed = [
            element
            for element in register_paragraphs
            if element is not keep
        ]

        run_test(
            (
                "remove all Register paragraphs except "
                f"#{keep_index + 1}"
            ),
            removed,
        )

    # ------------------------------------------------------------
    # Test 5:
    #
    # Remove ALL Register paragraphs.
    # ------------------------------------------------------------

    print()
    print("=" * 70)
    print(
        "TEST 4: REMOVE ALL REGISTER PARAGRAPHS"
    )
    print("=" * 70)

    run_test(
        "remove ALL Register paragraphs",
        register_paragraphs,
    )

    # ------------------------------------------------------------
    # Test 6:
    #
    # Remove title + first N Register paragraphs.
    # ------------------------------------------------------------

    if register_title is not None:

        # print()
        # print("=" * 70)
        # print(
        #     "TEST 5: TITLE + FIRST N REGISTER PARAGRAPHS"
        # )
        # print("=" * 70)

        # for count in paragraph_to_remove_counts:
        #     run_test(
        #         (
        #             "remove title + first "
        #             f"{count} Register paragraph(s)"
        #         ),
        #         [register_title]
        #         + register_paragraphs[:count],
        #     )

        print()
        print("=" * 70)
        print("TEST 5: TITLE + REGISTER PARAGRAPHS")
        print("=" * 70)

        run_decreasing_removal_tests(
            register_paragraphs,
            run_test,
            extra_removed=[register_title],
            label_prefix="remove title + first",
        )

        # --------------------------------------------------------
        # Test 7:
        #
        # Remove title + all Register paragraphs.
        # --------------------------------------------------------

        print()
        print("=" * 70)
        print(
            "TEST 6: TITLE + ALL REGISTER PARAGRAPHS"
        )
        print("=" * 70)

        run_test(
            "remove title + ALL Register paragraphs",
            [register_title]
            + register_paragraphs,
        )

    print()
    print("=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
