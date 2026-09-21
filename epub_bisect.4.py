#!/usr/bin/env python3

import argparse
import copy
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET


HTML_EXTENSIONS = {".html", ".htm", ".xhtml"}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(message: str = "") -> None:
    print(message, flush=True)


def die(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# EPUB filesystem helpers
# ---------------------------------------------------------------------------

def validate_epub_dir(root: Path) -> None:
    if not root.is_dir():
        die(f"Not a directory: {root}")

    mimetype = root / "mimetype"
    container = root / "META-INF" / "container.xml"

    if not mimetype.exists():
        die(f"Directory does not look like an EPUB: missing {mimetype}")

    if not container.exists():
        die(f"Directory does not look like an EPUB: missing {container}")


def unpack_epub(epub_path: Path, destination: Path) -> None:
    log(f"Unpacking EPUB: {epub_path}")

    with zipfile.ZipFile(epub_path, "r") as zf:
        zf.extractall(destination)

    validate_epub_dir(destination)


def copy_epub_directory(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination)
    validate_epub_dir(destination)


def zip_epub_directory(source_dir: Path, output_epub: Path) -> None:
    """
    Create a valid EPUB ZIP.

    The mimetype file must be the first entry and must be stored
    uncompressed according to the EPUB container requirements.
    """
    output_epub.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(
        output_epub,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as zf:

        mimetype = source_dir / "mimetype"

        if not mimetype.exists():
            raise RuntimeError(f"Missing mimetype in {source_dir}")

        zf.write(
            mimetype,
            "mimetype",
            compress_type=zipfile.ZIP_STORED,
        )

        for path in sorted(source_dir.rglob("*")):
            if path.is_dir():
                continue

            rel = path.relative_to(source_dir)

            if str(rel) == "mimetype":
                continue

            zf.write(
                path,
                str(rel).replace(os.sep, "/"),
                compress_type=zipfile.ZIP_DEFLATED,
            )


def make_epub_with_replaced_file(
    source_dir: Path,
    relative_path: Path,
    new_bytes: bytes,
    output_epub: Path,
) -> None:
    """
    Make a complete EPUB copy while replacing exactly one file.
    """
    with tempfile.TemporaryDirectory(prefix="epub-min-copy-") as td:
        temp_root = Path(td) / "epub"
        copy_epub_directory(source_dir, temp_root)

        target = temp_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(new_bytes)

        zip_epub_directory(temp_root, output_epub)


def make_epub_with_multiple_replacements(
    source_dir: Path,
    replacements: dict[Path, bytes],
    output_epub: Path,
) -> None:
    """
    Make a complete EPUB copy while replacing multiple files.
    """
    with tempfile.TemporaryDirectory(prefix="epub-min-copy-") as td:
        temp_root = Path(td) / "epub"
        copy_epub_directory(source_dir, temp_root)

        for relative_path, data in replacements.items():
            target = temp_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        zip_epub_directory(temp_root, output_epub)


def list_html_files(root: Path) -> list[Path]:
    files = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        if path.suffix.lower() in HTML_EXTENSIONS:
            files.append(path.relative_to(root))

    return sorted(files)


# ---------------------------------------------------------------------------
# XHTML helpers
# ---------------------------------------------------------------------------

def parse_xhtml(data: bytes) -> ET.ElementTree:
    """
    Parse XHTML as XML.

    EPUB XHTML should be XML-well-formed.
    """
    root = ET.fromstring(data)
    return ET.ElementTree(root)


def serialize_xhtml(tree: ET.ElementTree) -> bytes:
    return ET.tostring(
        tree.getroot(),
        encoding="utf-8",
        xml_declaration=True,
    )


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]

    return tag


def find_body(root: ET.Element) -> Optional[ET.Element]:
    for element in root.iter():
        if local_name(element.tag) == "body":
            return element

    return None


def get_element_by_path(
    root: ET.Element,
    path: tuple[int, ...],
) -> ET.Element:
    element = root

    for index in path:
        element = list(element)[index]

    return element


def find_element_path(
    root: ET.Element,
    target: ET.Element,
) -> Optional[tuple[int, ...]]:
    """
    Return a tuple of child indexes locating target from root.
    """

    def recurse(
        current: ET.Element,
        path: tuple[int, ...],
    ) -> Optional[tuple[int, ...]]:

        if current is target:
            return path

        for index, child in enumerate(list(current)):
            result = recurse(child, path + (index,))

            if result is not None:
                return result

        return None

    return recurse(root, ())


def clone_tree(tree: ET.ElementTree) -> ET.ElementTree:
    return ET.ElementTree(copy.deepcopy(tree.getroot()))


def remove_element_preserving_tail(
    root: ET.Element,
    path: tuple[int, ...],
) -> None:
    """
    Remove one element while preserving its tail text where practical.
    """

    if not path:
        raise ValueError("Cannot remove document root")

    parent_path = path[:-1]
    index = path[-1]

    parent = get_element_by_path(root, parent_path)
    children = list(parent)

    target = children[index]
    tail = target.tail

    if index > 0:
        previous = children[index - 1]
        previous.tail = (previous.tail or "") + (tail or "")
    else:
        parent.text = (parent.text or "") + (tail or "")

    parent.remove(target)


def remove_paths(
    tree: ET.ElementTree,
    paths: list[tuple[int, ...]],
) -> None:
    """
    Remove several elements.

    Paths are removed deepest-first and right-to-left so that
    sibling indexes remain valid.
    """
    root = tree.getroot()

    for path in sorted(
        paths,
        key=lambda p: (len(p), p),
        reverse=True,
    ):
        remove_element_preserving_tail(root, path)


def blank_html(tree: ET.ElementTree) -> ET.ElementTree:
    """
    Blank the body of an XHTML document while leaving the rest of
    the document structure untouched.
    """
    result = clone_tree(tree)
    body = find_body(result.getroot())

    if body is None:
        raise RuntimeError("XHTML document has no <body>")

    for child in list(body):
        remove_element_preserving_tail(
            result.getroot(),
            find_element_path(result.getroot(), child),
        )

    return result


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def list_processes() -> set[int]:
    """
    Return currently running PIDs.

    Using ps rather than /proc keeps this reasonably portable.
    """
    try:
        output = subprocess.check_output(
            ["ps", "-e", "-o", "pid="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return set()

    result = set()

    for line in output.splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            result.add(int(line))
        except ValueError:
            pass

    return result


def process_cmdline(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "args="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def process_name(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "comm="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def find_new_okular_process(
    before: set[int],
    reader_command: list[str],
    timeout: float = 1.0,
) -> Optional[int]:
    """
    Find the actual Okular worker process.

    On NixOS the process consuming CPU is commonly named
    .okular-wrapped even though the command line looks like:

        okular file.epub
    """

    deadline = time.monotonic() + timeout

    reader_basename = Path(reader_command[0]).name.lower()

    while time.monotonic() < deadline:
        current = list_processes()
        new_pids = sorted(current - before)

        # Prefer the actual NixOS wrapped worker.
        for pid in new_pids:
            name = process_name(pid).lower()

            if ".okular-wrapped" in name:
                return pid

        # Generic okular process fallback.
        for pid in new_pids:
            name = process_name(pid).lower()
            cmd = process_cmdline(pid).lower()

            if name == "okular" or " okular " in f" {cmd} ":
                return pid

        # Reader-specific fallback.
        for pid in new_pids:
            name = process_name(pid).lower()

            if reader_basename and reader_basename in name:
                return pid

        time.sleep(0.01)

    return None


def safe_terminate_pid(pid: Optional[int]) -> None:
    if pid is None:
        return

    if pid <= 0:
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        return
    except OSError:
        return

    deadline = time.monotonic() + 1.0

    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            return

        time.sleep(0.05)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Benchmarking
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkResult:
    bad: bool
    peak_cpu: float
    samples: list[float]
    pid: Optional[int]
    elapsed: float


def benchmark_epub(
    epub_path: Path,
    reader_command: list[str],
    timeout: float = 5.0,
    interval: float = 0.05,
    idle_threshold: float = 5.0,
    idle_samples: int = 3,
    verbose: bool = True,
) -> BenchmarkResult:
    """
    Open EPUB in the reader and classify it.

    GOOD:
        CPU falls to <= idle_threshold for idle_samples consecutive
        measurements.

    BAD:
        CPU never becomes idle before timeout.

    There is deliberately NO fixed warmup and NO fixed benchmark
    duration. Monitoring starts as soon as the actual reader process
    is found.
    """

    try:
        import psutil
    except ImportError:
        die(
            "python3-psutil is required.\n"
            "On NixOS, try adding python3Packages.psutil to your environment."
        )

    before = list_processes()

    started = time.monotonic()

    if verbose:
        log(f"    Opening: {epub_path.name}")

    try:
        launcher = subprocess.Popen(
            reader_command + [str(epub_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        if verbose:
            log(f"    Could not start reader: {exc}")

        return BenchmarkResult(
            bad=False,
            peak_cpu=0.0,
            samples=[],
            pid=None,
            elapsed=time.monotonic() - started,
        )

    # Find the process that actually does the rendering/work.
    target_pid = find_new_okular_process(
        before,
        reader_command,
        timeout=min(1.0, timeout),
    )

    # If we cannot identify a child, use the launcher as a fallback.
    if target_pid is None:
        if launcher.poll() is None:
            target_pid = launcher.pid

    if target_pid is None:
        safe_terminate_pid(launcher.pid)

        return BenchmarkResult(
            bad=False,
            peak_cpu=0.0,
            samples=[],
            pid=None,
            elapsed=time.monotonic() - started,
        )

    try:
        target = psutil.Process(target_pid)
    except psutil.Error:
        safe_terminate_pid(target_pid)

        if launcher.pid != target_pid:
            safe_terminate_pid(launcher.pid)

        return BenchmarkResult(
            bad=False,
            peak_cpu=0.0,
            samples=[],
            pid=target_pid,
            elapsed=time.monotonic() - started,
        )

    # Prime psutil immediately.
    #
    # This does NOT wait for a warmup period. The next cpu_percent()
    # gives us the CPU usage over the first sampling interval.
    try:
        target.cpu_percent(None)
    except psutil.Error:
        pass

    samples: list[float] = []
    peak_cpu = 0.0
    consecutive_idle = 0

    # The timeout starts when CPU monitoring begins, not after a
    # fixed warmup period.
    monitoring_started = time.monotonic()
    deadline = monitoring_started + timeout

    bad = True

    try:
        while True:
            now = time.monotonic()

            if now >= deadline:
                bad = True
                break

            sleep_time = min(
                interval,
                max(0.0, deadline - now),
            )

            if sleep_time > 0:
                time.sleep(sleep_time)

            try:
                if not target.is_running():
                    # Reader exited by itself: healthy behavior.
                    cpu = 0.0

                    samples.append(cpu)

                    if verbose:
                        log("    Reader exited normally -> GOOD")

                    bad = False
                    break

                cpu = target.cpu_percent(None)

            except psutil.NoSuchProcess:
                # Process disappeared, so it clearly isn't burning CPU.
                cpu = 0.0

                samples.append(cpu)

                if verbose:
                    log("    Reader process exited -> GOOD")

                bad = False
                break

            except psutil.Error:
                cpu = 0.0

            samples.append(cpu)
            peak_cpu = max(peak_cpu, cpu)

            if verbose:
                log(f"    CPU: {cpu:6.1f}%")

            if cpu <= idle_threshold:
                consecutive_idle += 1

                if consecutive_idle >= idle_samples:
                    bad = False
                    break

            else:
                consecutive_idle = 0

    finally:
        # IMPORTANT:
        # Kill the actual worker directly.
        #
        # Do not kill the process group. On NixOS the PGID can be
        # surprising and can cause unrelated processes to be killed.
        safe_terminate_pid(target_pid)

        if launcher.pid != target_pid:
            safe_terminate_pid(launcher.pid)

    elapsed = time.monotonic() - started

    return BenchmarkResult(
        bad=bad,
        peak_cpu=peak_cpu,
        samples=samples,
        pid=target_pid,
        elapsed=elapsed,
    )


# ---------------------------------------------------------------------------
# Minimizer
# ---------------------------------------------------------------------------

class EPUBMinimizer:

    def __init__(
        self,
        source_dir: Path,
        output_dir: Path,
        reader_command: list[str],
        timeout: float,
        interval: float,
        idle_threshold: float,
        idle_samples: int,
        keep_tests: bool,
        max_tests: Optional[int],
    ):
        self.source_dir = source_dir
        self.output_dir = output_dir
        self.reader_command = reader_command

        self.timeout = timeout
        self.interval = interval
        self.idle_threshold = idle_threshold
        self.idle_samples = idle_samples

        self.keep_tests = keep_tests
        self.max_tests = max_tests

        self.test_number = 0

        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.cache: dict[bytes, BenchmarkResult] = {}

    # ------------------------------------------------------------------
    # Test management
    # ------------------------------------------------------------------

    def next_test_path(self, prefix: str, suffix: str = ".epub") -> Path:
        self.test_number += 1

        if (
            self.max_tests is not None
            and self.test_number > self.max_tests
        ):
            die(
                f"Maximum test count ({self.max_tests}) exceeded."
            )

        return self.output_dir / (
            f"test-{self.test_number:05d}-{prefix}{suffix}"
        )

    def benchmark_candidate(
        self,
        epub_path: Path,
        candidate_key: Optional[bytes] = None,
    ) -> BenchmarkResult:

        if candidate_key is not None:
            cached = self.cache.get(candidate_key)

            if cached is not None:
                log("    cached result")
                return cached

        result = benchmark_epub(
            epub_path,
            self.reader_command,
            timeout=self.timeout,
            interval=self.interval,
            idle_threshold=self.idle_threshold,
            idle_samples=self.idle_samples,
            verbose=True,
        )

        if candidate_key is not None:
            self.cache[candidate_key] = result

        if result.bad:
            status = "BAD"
        else:
            status = "GOOD"

        log(
            f"    => {status} "
            f"(peak {result.peak_cpu:.1f}%, "
            f"{result.elapsed:.2f}s)"
        )

        if not self.keep_tests:
            try:
                epub_path.unlink()
            except FileNotFoundError:
                pass

        return result

    # ------------------------------------------------------------------
    # File isolation
    # ------------------------------------------------------------------

    def identify_problematic_files(
        self,
        html_paths: list[Path],
        parsed: dict[Path, ET.ElementTree],
    ) -> list[Path]:

        log()
        log("=" * 72)
        log("AUTOMATIC HTML/XHTML ISOLATION")
        log("=" * 72)

        log(
            "For each candidate:"
            "\n"
            "  * candidate HTML/XHTML remains ORIGINAL"
            "\n"
            "  * every other HTML/XHTML file is blanked"
            "\n"
            "  * all non-HTML resources remain unchanged"
        )

        problematic: list[Path] = []

        for selected in html_paths:
            log()
            log(f"[FILE TEST] {selected}")

            replacements: dict[Path, bytes] = {}

            for rel in html_paths:
                if rel == selected:
                    # This is the important part:
                    # selected file remains completely original.
                    continue

                blanked = blank_html(parsed[rel])

                replacements[rel] = serialize_xhtml(blanked)

            candidate = self.next_test_path(
                f"file-isolation-{selected.stem}"
            )

            make_epub_with_multiple_replacements(
                self.source_dir,
                replacements,
                candidate,
            )

            result = self.benchmark_candidate(candidate)

            if result.bad:
                log(f"    *** SUFFICIENT: {selected}")
                problematic.append(selected)
            else:
                log(f"    insufficient: {selected}")

        return problematic

    # ------------------------------------------------------------------
    # Delta-debugging helpers
    # ------------------------------------------------------------------

    @staticmethod
    def chunks(
        items: list,
        n: int,
    ) -> list[list]:

        if not items:
            return []

        n = max(1, min(n, len(items)))

        result = []

        for i in range(n):
            start = len(items) * i // n
            end = len(items) * (i + 1) // n

            if start < end:
                result.append(items[start:end])

        return result

    def test_tree(
        self,
        tree: ET.ElementTree,
        target_path: Path,
        prefix: str,
    ) -> BenchmarkResult:

        data = serialize_xhtml(tree)

        candidate = self.next_test_path(prefix)

        make_epub_with_replaced_file(
            self.source_dir,
            target_path,
            data,
            candidate,
        )

        # The serialized XHTML itself is a perfect cache key for
        # structural minimization.
        return self.benchmark_candidate(
            candidate,
            candidate_key=data,
        )

    # ------------------------------------------------------------------
    # Direct-child delta debugging
    # ------------------------------------------------------------------

    def minimize_children(
        self,
        tree: ET.ElementTree,
        parent_path: tuple[int, ...],
        target_path: Path,
        label: str,
    ) -> bool:
        """
        Minimize the direct children of one element.

        This is essentially hierarchical delta debugging:

          1. Divide children into chunks.
          2. Remove one chunk.
          3. If still BAD, keep the deletion.
          4. Increase/decrease granularity as appropriate.
          5. Repeat until no more deletions work.

        This works even if the body has only ONE direct child, because
        we recursively enter that child.
        """

        changed_any = False
        granularity = 2

        while True:
            parent = get_element_by_path(
                tree.getroot(),
                parent_path,
            )

            children = list(parent)

            if not children:
                break

            if len(children) == 1:
                granularity = 1
            else:
                granularity = min(
                    granularity,
                    len(children),
                )

            child_paths = [
                parent_path + (index,)
                for index in range(len(children))
            ]

            groups = self.chunks(
                child_paths,
                granularity,
            )

            made_progress = False

            for group_index, group in enumerate(groups):
                log()
                log(
                    f"    Trying deletion group "
                    f"{group_index + 1}/{len(groups)} "
                    f"from {label}"
                )

                candidate_tree = clone_tree(tree)

                remove_paths(
                    candidate_tree,
                    group,
                )

                result = self.test_tree(
                    candidate_tree,
                    target_path,
                    f"{label}-delete",
                )

                if result.bad:
                    log(
                        f"    ACCEPT deletion of "
                        f"{len(group)} element(s)"
                    )

                    tree._setroot(
                        copy.deepcopy(candidate_tree.getroot())
                    )

                    changed_any = True
                    made_progress = True
                    break

                log("    Keep those elements")

            if made_progress:
                # Recompute the current child list because indexes
                # changed after the accepted deletion.
                parent = get_element_by_path(
                    tree.getroot(),
                    parent_path,
                )

                children = list(parent)

                if not children:
                    break

                # After successful deletion, try the same granularity
                # again before making groups smaller.
                granularity = min(
                    granularity,
                    len(children),
                )

                continue

            # No deletion worked at this granularity.
            if granularity >= len(children):
                break

            granularity = min(
                len(children),
                granularity * 2,
            )

        return changed_any

    # ------------------------------------------------------------------
    # Recursive minimization
    # ------------------------------------------------------------------

    def minimize_recursive(
        self,
        tree: ET.ElementTree,
        element_path: tuple[int, ...],
        target_path: Path,
        depth: int = 0,
    ) -> None:

        element = get_element_by_path(
            tree.getroot(),
            element_path,
        )

        name = local_name(element.tag)

        indent = "  " * depth

        log()
        log(
            f"{indent}MINIMIZE <{name}> "
            f"path={element_path}"
        )

        # First minimize this element's direct children.
        self.minimize_children(
            tree,
            element_path,
            target_path,
            label=f"depth-{depth}-{name}",
        )

        # Now recursively minimize each surviving child.
        #
        # Recursive minimization never removes the child itself.
        # It only removes descendants, so its sibling index remains
        # valid.
        index = 0

        while True:
            current = get_element_by_path(
                tree.getroot(),
                element_path,
            )

            children = list(current)

            if index >= len(children):
                break

            child_path = element_path + (index,)

            self.minimize_recursive(
                tree,
                child_path,
                target_path,
                depth + 1,
            )

            index += 1

    # ------------------------------------------------------------------
    # Complete minimization
    # ------------------------------------------------------------------

    def minimize_file(
        self,
        target_path: Path,
    ) -> ET.ElementTree:

        log()
        log("=" * 72)
        log(f"STRUCTURAL MINIMIZATION: {target_path}")
        log("=" * 72)

        original_data = (
            self.source_dir / target_path
        ).read_bytes()

        tree = parse_xhtml(original_data)

        root = tree.getroot()

        body = find_body(root)

        if body is None:
            die(
                f"{target_path} does not contain a <body>"
            )

        body_path = find_element_path(
            root,
            body,
        )

        if body_path is None:
            die(
                f"Could not determine path to <body> "
                f"in {target_path}"
            )

        log(f"Body path: {body_path}")
        log(f"Initial body children: {len(list(body))}")

        # First verify that the original document is actually BAD.
        log()
        log("Verifying original problematic EPUB...")

        original_test = self.next_test_path(
            "original-verification"
        )

        shutil.copy2(
            self.source_dir.parent / "__nonexistent__",
            original_test,
        ) if False else zip_epub_directory(
            self.source_dir,
            original_test,
        )

        result = self.benchmark_candidate(original_test)

        if not result.bad:
            die(
                "The supplied EPUB no longer reproduces the CPU bug."
            )

        log("Original EPUB confirmed BAD.")

        # Recursively minimize from <body>.
        self.minimize_recursive(
            tree,
            body_path,
            target_path,
        )

        return tree

    # ------------------------------------------------------------------
    # Final output
    # ------------------------------------------------------------------

    def write_final(
        self,
        target_path: Path,
        tree: ET.ElementTree,
    ) -> Path:

        final_path = self.output_dir / "FINAL-minimized.epub"

        data = serialize_xhtml(tree)

        make_epub_with_replaced_file(
            self.source_dir,
            target_path,
            data,
            final_path,
        )

        return final_path


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_reader_command(value: str) -> list[str]:
    """
    Simple reader command parser.

    Example:

        --epub-reader okular

    or:

        --epub-reader "okular --some-option"

    For more complex commands, use a wrapper script.
    """
    import shlex

    command = shlex.split(value)

    if not command:
        die("--epub-reader cannot be empty")

    return command


def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Minimize an EPUB that causes an EPUB reader to burn CPU."
        )
    )

    parser.add_argument(
        "input",
        type=Path,
        help=(
            "Broken EPUB file or unpacked EPUB directory"
        ),
    )

    parser.add_argument(
        "--html-path",
        type=Path,
        default=None,
        help=(
            "Specific HTML/XHTML file to minimize, relative to "
            "the EPUB root. If omitted, automatically test every "
            "HTML/XHTML file."
        ),
    )

    parser.add_argument(
        "--epub-reader",
        default="okular",
        help=(
            "Reader command used to open EPUBs "
            "(default: okular)"
        ),
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("epub-minimizer-tests"),
        help=(
            "Directory for generated test EPUBs "
            "(default: epub-minimizer-tests)"
        ),
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help=(
            "Maximum seconds to wait for CPU to become idle "
            "(default: 5)"
        ),
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=0.05,
        help=(
            "CPU sampling interval in seconds "
            "(default: 0.05)"
        ),
    )

    parser.add_argument(
        "--idle-threshold",
        type=float,
        default=5.0,
        help=(
            "CPU percentage regarded as idle "
            "(default: 5)"
        ),
    )

    parser.add_argument(
        "--idle-samples",
        type=int,
        default=3,
        help=(
            "Consecutive idle samples required for GOOD "
            "(default: 3)"
        ),
    )

    parser.add_argument(
        "--keep-tests",
        action="store_true",
        help=(
            "Keep every generated test EPUB. By default "
            "temporary test EPUBs are deleted after testing."
        ),
    )

    parser.add_argument(
        "--max-tests",
        type=int,
        default=None,
        help=(
            "Abort after this many benchmark tests."
        ),
    )

    args = parser.parse_args()

    if args.timeout <= 0:
        die("--timeout must be greater than zero")

    if args.interval <= 0:
        die("--interval must be greater than zero")

    if args.idle_samples <= 0:
        die("--idle-samples must be greater than zero")

    if args.idle_threshold < 0:
        die("--idle-threshold cannot be negative")

    if args.max_tests is not None and args.max_tests <= 0:
        die("--max-tests must be greater than zero")

    input_path = args.input.resolve()

    # ---------------------------------------------------------------
    # Prepare EPUB source directory
    # ---------------------------------------------------------------

    with tempfile.TemporaryDirectory(
        prefix="epub-minimizer-source-"
    ) as source_temp:

        source_temp_path = Path(source_temp)

        if input_path.is_dir():
            validate_epub_dir(input_path)

            source_dir = source_temp_path / "epub"

            copy_epub_directory(
                input_path,
                source_dir,
            )

        elif input_path.is_file():
            if input_path.suffix.lower() != ".epub":
                die(
                    f"Input file is not an EPUB: {input_path}"
                )

            source_dir = source_temp_path / "epub"

            source_dir.mkdir(parents=True)

            unpack_epub(
                input_path,
                source_dir,
            )

        else:
            die(
                f"Input does not exist: {input_path}"
            )

        # ---------------------------------------------------------------
        # Find HTML/XHTML files
        # ---------------------------------------------------------------

        html_paths = list_html_files(source_dir)

        if not html_paths:
            die(
                "No .html, .htm, or .xhtml files found in EPUB."
            )

        log()
        log("=" * 72)
        log("EPUB CPU MINIMIZER")
        log("=" * 72)

        log(f"Input: {input_path}")
        log(f"HTML/XHTML files: {len(html_paths)}")
        log(f"Reader: {args.epub_reader}")
        log(f"Timeout: {args.timeout}s")
        log(f"CPU interval: {args.interval}s")
        log(f"Idle threshold: {args.idle_threshold:.1f}%")
        log(f"Idle samples: {args.idle_samples}")

        # ---------------------------------------------------------------
        # Parse all HTML files once.
        # ---------------------------------------------------------------

        parsed: dict[Path, ET.ElementTree] = {}

        for rel in html_paths:
            path = source_dir / rel

            try:
                parsed[rel] = parse_xhtml(
                    path.read_bytes()
                )
            except Exception as exc:
                log()
                log(
                    f"WARNING: Cannot parse {rel}: {exc}"
                )

        if args.html_path is not None:
            target_path = args.html_path

            if target_path.is_absolute():
                try:
                    target_path = target_path.relative_to(
                        source_dir
                    )
                except ValueError:
                    die(
                        "--html-path must be relative to "
                        "the EPUB root"
                    )

            target_path = Path(
                str(target_path).lstrip("./")
            )

            if target_path not in html_paths:
                die(
                    f"--html-path not found in EPUB: "
                    f"{target_path}"
                )

            if target_path not in parsed:
                die(
                    f"Could not parse target XHTML: "
                    f"{target_path}"
                )

            problematic_files = [target_path]

            log()
            log(
                f"Using explicitly specified target: "
                f"{target_path}"
            )

        else:
            # -----------------------------------------------------------
            # Automatic file isolation
            # -----------------------------------------------------------

            minimizer = EPUBMinimizer(
                source_dir=source_dir,
                output_dir=args.output_dir.resolve(),
                reader_command=parse_reader_command(
                    args.epub_reader
                ),
                timeout=args.timeout,
                interval=args.interval,
                idle_threshold=args.idle_threshold,
                idle_samples=args.idle_samples,
                keep_tests=args.keep_tests,
                max_tests=args.max_tests,
            )

            parsed_for_isolation = {
                rel: tree
                for rel, tree in parsed.items()
            }

            problematic_files = (
                minimizer.identify_problematic_files(
                    list(parsed_for_isolation.keys()),
                    parsed_for_isolation,
                )
            )

            if not problematic_files:
                die(
                    "No individual HTML/XHTML file was sufficient "
                    "to reproduce the CPU problem."
                )

            log()
            log("=" * 72)
            log("SUFFICIENT HTML/XHTML FILE(S)")
            log("=" * 72)

            for rel in problematic_files:
                log(f"  {rel}")

            # If multiple files independently reproduce the bug,
            # minimize each one. Usually there will only be one.
            target_path = problematic_files[0]

            if len(problematic_files) > 1:
                log()
                log(
                    "WARNING: Multiple HTML/XHTML files independently "
                    "trigger the bug."
                )
                log(
                    f"Minimizing the first one: {target_path}"
                )

        # ---------------------------------------------------------------
        # Structural minimization
        # ---------------------------------------------------------------

        minimizer = EPUBMinimizer(
            source_dir=source_dir,
            output_dir=args.output_dir.resolve(),
            reader_command=parse_reader_command(
                args.epub_reader
            ),
            timeout=args.timeout,
            interval=args.interval,
            idle_threshold=args.idle_threshold,
            idle_samples=args.idle_samples,
            keep_tests=args.keep_tests,
            max_tests=args.max_tests,
        )

        minimized_tree = minimizer.minimize_file(
            target_path
        )

        # ---------------------------------------------------------------
        # Write final EPUB
        # ---------------------------------------------------------------

        final_path = minimizer.write_final(
            target_path,
            minimized_tree,
        )

        log()
        log("=" * 72)
        log("DONE")
        log("=" * 72)
        log(f"Final minimized EPUB:")
        log(f"  {final_path.resolve()}")
        log()
        log(f"Total benchmark tests: {minimizer.test_number}")


if __name__ == "__main__":
    main()
