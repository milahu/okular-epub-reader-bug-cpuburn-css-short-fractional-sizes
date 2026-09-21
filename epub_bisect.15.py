#!/usr/bin/env python3

# TODO also test other sizes than font-size
# examples: margin, padding, line-height, left, top

import argparse
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
import random

def create_synthetic_epub(
        output_epub,
        paragraph_count,
        font_size=".5em",
        external_css=False,
        complete_register_rule=False,
    ):
    """
    Create a synthetic EPUB containing `paragraph_count` Register
    paragraphs.

    If external_css=False, the CSS is embedded in <style>.

    If external_css=True, the CSS is written to OEBPS/styles.css
    and referenced using <link rel="stylesheet">.

    If complete_register_rule=True, reproduce the original Register
    rule instead of just font-size.
    """

    output_epub = Path(output_epub)

    xhtml_paragraphs = "\n".join(
        f'    <p class="Register">Paragraph {i}</p>'
        for i in range(1, paragraph_count + 1)
    )

    if complete_register_rule:
        css = (
            "p.Register {\n"
            "  text-indent: -1em;\n"
            "  text-align: left;\n"
            "  padding-left: 1em;\n"
            f"  font-size: {font_size};\n"
            "}\n"
        )
    else:
        css = (
            "p.Register {\n"
            f"  font-size: {font_size};\n"
            "}\n"
        )

    if external_css:
        head_css = (
            '  <link rel="stylesheet" type="text/css" '
            'href="styles.css"/>\n'
        )
    else:
        head_css = (
            '  <style type="text/css">\n'
            f"{css}"
            "  </style>\n"
        )

    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<container version="1.0"\n'
        '    xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        "    <rootfile\n"
        '        full-path="OEBPS/content.opf"\n'
        '        media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    )

    if external_css:
        manifest_extra = (
            "    <item\n"
            '        id="styles"\n'
            '        href="styles.css"\n'
            '        media-type="text/css"/>\n'
        )
    else:
        manifest_extra = ""

    content_opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package\n'
        '    xmlns="http://www.idpf.org/2007/opf"\n'
        '    version="2.0"\n'
        '    unique-identifier="BookId">\n'
        "\n"
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        "    <dc:title>Synthetic EPUB</dc:title>\n"
        "    <dc:language>en</dc:language>\n"
        '    <dc:identifier id="BookId">synthetic-test</dc:identifier>\n'
        "  </metadata>\n"
        "\n"
        "  <manifest>\n"
        "    <item\n"
        '        id="content"\n'
        '        href="content.xhtml"\n'
        '        media-type="application/xhtml+xml"/>\n'
        f"{manifest_extra}"
        "  </manifest>\n"
        "\n"
        "  <spine>\n"
        '    <itemref idref="content"/>\n'
        "  </spine>\n"
        "\n"
        "</package>\n"
    )

    content_xhtml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">\n'
        "\n"
        "<head>\n"
        "  <title>Synthetic EPUB</title>\n"
        "\n"
        f"{head_css}"
        "</head>\n"
        "\n"
        "<body>\n"
        f"{xhtml_paragraphs}\n"
        "</body>\n"
        "\n"
        "</html>\n"
    )

    output_epub.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(
        output_epub,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as z:

        # EPUB requires mimetype to be the first entry and uncompressed.
        z.writestr(
            "mimetype",
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )

        z.writestr(
            "META-INF/container.xml",
            container_xml,
        )

        z.writestr(
            "OEBPS/content.opf",
            content_opf,
        )

        z.writestr(
            "OEBPS/content.xhtml",
            content_xhtml,
        )

        if external_css:
            z.writestr(
                "OEBPS/styles.css",
                css,
            )


def test_synthetic_epubs(
        output_dir,
        epub_reader,
        good_start=1,
        random_seed=None,
        font_size=".5em",

        # FIXME use a dynamic timeout value
        # use double the time of the previous good step
        # example:
        # N=10 -> step takes 1 second
        # N=20 -> use timeout=2 for this step

        # timeout=20,
        # timeout=5, # good: 1 second
        # timeout=10, # good: 1 second
        timeout=30, # good: 1 second

        interval=0.05,
        idle_threshold=5,
        idle_samples=3,
    ):
    """
    Find the paragraph-count threshold at which the synthetic EPUB
    becomes pathological.

    Search strategy:

      1. Start at `good_start`.
      2. Exponentially increase the count:
             good + 1
             good + 2
             good + 4
             good + 8
             ...
      3. Stop at the first BAD result.
      4. Binary-search the GOOD/BAD bracket.

    Returns:
        {
            "good": largest known GOOD paragraph count,
            "bad": smallest known BAD paragraph count,
        }
    """

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print()
    print("=" * 70)
    print("SYNTHETIC EPUB THRESHOLD TEST")
    print("=" * 70)
    print(f"font-size: {font_size}")
    print(f"timeout:    {timeout}s")

    # ------------------------------------------------------------
    # Helper for running one synthetic EPUB.
    # ------------------------------------------------------------

    def run_test(count, phase):
        epub_path = output_dir / f"synthetic-{count}.epub"

        create_synthetic_epub(
            epub_path,
            paragraph_count=count,
            font_size=font_size,
            external_css=True,
            complete_register_rule=False,
        )

        result = benchmark_epub(
            epub_path,
            epub_reader=epub_reader,
            timeout=timeout,
            interval=interval,
            idle_threshold=idle_threshold,
            idle_samples=idle_samples,
        )

        status = "GOOD" if result.good else "BAD"

        print(
            f"[{status}] "
            f"{phase:10s} "
            f"{count:5d} paragraphs "
            f"{result.elapsed:7.2f}s "
            f"peak={result.peak_cpu:6.1f}%"
        )

        return result

    # ------------------------------------------------------------
    # Establish the initial GOOD point.
    # ------------------------------------------------------------

    result = run_test(good_start, "start")

    if not result.good:
        raise RuntimeError(
            f"Starting point {good_start} is already BAD"
        )

    good = good_start
    bad = None

    # ------------------------------------------------------------
    # Phase 1: exponential search.
    #
    # Counts:
    #
    #   good_start + 1
    #   good_start + 2
    #   good_start + 4
    #   good_start + 8
    #   ...
    # ------------------------------------------------------------

    increment = 1

    while True:
        count = good_start + increment

        result = run_test(count, "exponential")

        if result.good:
            good = count
            increment *= 2
            continue

        bad = count

        print()
        print(
            f"[BRACKET] GOOD={good}, BAD={bad}"
        )

        break

    # ------------------------------------------------------------
    # Phase 2: binary search.
    # ------------------------------------------------------------

    while bad - good > 1:
        count = (good + bad) // 2

        result = run_test(count, "binary")

        if result.good:
            good = count
        else:
            bad = count

        print(
            f"           bracket: GOOD={good}, BAD={bad}"
        )

    # ------------------------------------------------------------
    # Final result.
    # ------------------------------------------------------------

    print()
    print("=" * 70)
    print("THRESHOLD FOUND")
    print("=" * 70)
    print(f"Largest GOOD: {good} paragraphs")
    print(f"Smallest BAD:  {bad} paragraphs")
    print(f"Transition:    {good} -> {bad}")
    print("=" * 70)

    return {
        "good": good,
        "bad": bad,
    }


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


def test_register_selection_threshold(
        register_paragraphs,
        run_test,
        good_leaving,
        random_seed=12345,
    ):
    """
    For each selection mode:

      1. Start from a known GOOD count.
      2. Grow exponentially:
             good + 1
             good + 2
             good + 4
             good + 8
             ...
      3. Stop at the first BAD result.
      4. Binary-search the GOOD/BAD bracket until delta == 1.

    For random mode, one deterministic permutation is generated once
    and all tested sets are prefixes of that same permutation.
    """

    print()
    print("=" * 70)
    print("TEST: FIND THRESHOLD FOR REGISTER PARAGRAPH SELECTION")
    print("=" * 70)

    n = len(register_paragraphs)

    random_order = make_random_register_order(
        register_paragraphs,
        seed=random_seed,
    )

    print()
    print(f"Random seed: {random_seed}")
    print("Random ordering of Register paragraph indices:")
    print(random_order)

    results = {}

    for mode in ("first", "last", "random"):
        print()
        print("-" * 70)
        print(f"MODE: {mode}")
        print("-" * 70)

        # ------------------------------------------------------------
        # Phase 1: exponential search
        # ------------------------------------------------------------

        good = good_leaving
        bad = None

        increment = 1

        while True:
            count = good_leaving + increment

            if count > n:
                count = n

            if count <= good:
                break

            selected = select_register_paragraphs(
                register_paragraphs,
                count,
                mode,
                random_order=random_order,
            )

            selected_set = {
                (element.start, element.end)
                for element in selected
            }

            removed = [
                element
                for element in register_paragraphs
                if (element.start, element.end)
                not in selected_set
            ]

            result = run_test(
                f"keep {count} Register paragraphs ({mode})",
                removed,
            )

            if result.good:
                good = count
                increment *= 2
                continue

            # First BAD gives us the bracket.
            bad = count

            print(
                f"[BRACKET] {mode}: "
                f"GOOD={good}, BAD={bad}"
            )

            break

        if bad is None:
            print(
                f"[WARN] {mode}: "
                f"no BAD result found."
            )
            results[mode] = None
            continue

        # ------------------------------------------------------------
        # Phase 2: binary search
        # ------------------------------------------------------------

        while bad - good > 1:
            count = (good + bad) // 2

            selected = select_register_paragraphs(
                register_paragraphs,
                count,
                mode,
                random_order=random_order,
            )

            selected_set = {
                (element.start, element.end)
                for element in selected
            }

            removed = [
                element
                for element in register_paragraphs
                if (element.start, element.end)
                not in selected_set
            ]

            result = run_test(
                f"refine {mode}: keep {count} Register paragraphs",
                removed,
            )

            if result.good:
                good = count
            else:
                bad = count

            print(
                f"    bracket: GOOD={good}, BAD={bad}"
            )

        print()
        print(
            f"[RESULT] {mode}: "
            f"GOOD={good}, BAD={bad}"
        )

        results[mode] = {
            "good": good,
            "bad": bad,
        }

    return results


def make_random_register_order(register_paragraphs, seed=12345):
    indices = list(range(len(register_paragraphs)))

    rng = random.Random(seed)
    rng.shuffle(indices)

    return indices


def select_register_paragraphs(
        register_paragraphs,
        count,
        mode,
        random_order=None,
    ):
    if mode == "first":
        return register_paragraphs[:count]

    if mode == "last":
        return register_paragraphs[-count:]

    if mode == "random":
        indices = random_order[:count]

        selected = [
            register_paragraphs[i]
            for i in indices
        ]

        # Preserve original XHTML order.
        return sorted(
            selected,
            key=lambda element: element.start,
        )

    raise ValueError(f"Unknown selection mode: {mode}")


epub_reader = "okular"


test_synthetic_epubs(
    output_dir="synthetic-tests",
    epub_reader=epub_reader,

    # good: CPU load is low
    # bad: CPU load explodes to 100%
    # font_size=".1em", # bad: bracket: GOOD=272610, BAD=272611
    # font_size=".2em", # bad: bracket: GOOD=209700, BAD=209701
    # font_size=".3em", # bad: bracket: GOOD=157275, BAD=157276
    # font_size=".4em", # bad: bracket: GOOD=120578, BAD=120579
    # font_size=".5em", # bad: bracket: GOOD=94365, BAD=94366
    # font_size=".6em", # bad: bracket: GOOD=83880, BAD=83881
    # font_size=".7em", # bad: bracket: GOOD=73395, BAD=73396
    # font_size=".8em", # bad: bracket: GOOD=31954, BAD=31955
    # font_size=".9em", # bad: bracket: GOOD=31495, BAD=31496
    # font_size=".10em", # bad: bracket: GOOD=26217, BAD=26218
    # font_size=".11em", # bad: bracket: GOOD=26212, BAD=26213
    font_size=".12em", # bad: bracket: GOOD=20970, BAD=20971
    # font_size=".55em", # bad: bracket: GOOD=1053, BAD=1054
    # font_size=".85em", # bad: bracket: GOOD=612, BAD=613
    # font_size=".99em", # bad: bracket: GOOD=526, BAD=527
    # font_size=".88em", # bad: bracket: GOOD=591, BAD=592
    # font_size=".66em", # bad: bracket: GOOD=785, BAD=786
    # font_size=".777em", # bad: bracket: GOOD=72, BAD=73
    # font_size=".666em", # bad: bracket: GOOD=84, BAD=85

    # bad: characters are rendered as squares
    # font_size=".6666em", # bad
    # font_size=".7777em", # bad
    # font_size=".77777em", # bad
)
