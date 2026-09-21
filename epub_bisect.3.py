#!/usr/bin/env python3

import argparse
import os
import signal
import subprocess
import time

import psutil


def find_new_okular(before_pids):
    """
    Find newly-created .okular-wrapped / okular processes.
    """
    matches = []

    for proc in psutil.process_iter(
        ["pid", "ppid", "name", "cmdline"]
    ):
        try:
            if proc.pid in before_pids:
                continue

            name = (proc.info["name"] or "").lower()
            cmdline = " ".join(
                proc.info["cmdline"] or []
            ).lower()

            if (
                ".okular-wrapped" in name
                or ".okular-wrapped" in cmdline
                or name == "okular"
                or (
                    cmdline.startswith("okular ")
                    or cmdline == "okular"
                )
            ):
                matches.append(proc)

        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess,
        ):
            pass

    return matches


def kill_process(proc):
    """
    Terminate the specific Okular process.
    """
    try:
        proc.send_signal(signal.SIGTERM)
    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
    ):
        return

    try:
        proc.wait(timeout=0.5)
        return
    except (
        psutil.TimeoutExpired,
        psutil.NoSuchProcess,
    ):
        pass

    try:
        proc.kill()
    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
    ):
        pass


def benchmark(
    epub,
    warmup,
    duration,
    interval,
    threshold,
    consecutive_required,
):
    epub = os.path.abspath(epub)

    print()
    print("=" * 100)
    print(f"Testing: {os.path.basename(epub)}")
    print("=" * 100)

    print(f"  warmup       : {warmup:.2f}s")
    print(f"  duration     : {duration:.2f}s")
    print(f"  interval     : {interval:.2f}s")
    print(f"  threshold    : {threshold:.1f}%")

    # Snapshot processes that already exist.
    before_pids = {
        proc.pid
        for proc in psutil.process_iter(["pid"])
    }

    print("  starting Okular...")

    launcher = subprocess.Popen(
        [
            "okular",
            epub,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print(f"  launcher PID : {launcher.pid}")

    # Find the actual .okular-wrapped process.
    target = None

    deadline = time.monotonic() + 5.0

    while time.monotonic() < deadline:
        matches = find_new_okular(before_pids)

        if matches:
            # Prefer .okular-wrapped because that's the
            # process we know is doing the actual work.
            wrapped = [
                p for p in matches
                if ".okular-wrapped" in
                (p.info["name"] or "").lower()
            ]

            target = wrapped[0] if wrapped else matches[0]
            break

        time.sleep(0.02)

    if target is None:
        print("  ERROR: could not find Okular process")

        try:
            launcher.terminate()
        except ProcessLookupError:
            pass

        return False

    print(f"  target PID   : {target.pid}")

    try:
        print(f"  target name  : {target.name()}")
        print(f"  target PPID  : {target.ppid()}")
        print(f"  target PGID  : {os.getpgid(target.pid)}")
        print(f"  target SID   : {os.getsid(target.pid)}")
    except (
        psutil.NoSuchProcess,
        PermissionError,
    ):
        pass

    # Prime psutil's CPU measurement.
    try:
        target.cpu_percent(None)
    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
    ):
        pass

    print("  warming up...")
    time.sleep(warmup)

    # Prime again after warmup.
    try:
        target.cpu_percent(None)
    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
    ):
        pass

    print("  monitoring...")

    peak_cpu = 0.0
    bad_count = 0
    result_bad = False

    end_time = time.monotonic() + duration

    while time.monotonic() < end_time:
        time.sleep(interval)

        try:
            cpu = target.cpu_percent(None)
        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess,
        ):
            print("    Okular process exited")
            break

        peak_cpu = max(peak_cpu, cpu)

        print(
            f"    CPU: {cpu:7.1f}%"
            f"    peak: {peak_cpu:7.1f}%"
        )

        if cpu >= threshold:
            bad_count += 1

            if bad_count >= consecutive_required:
                result_bad = True
                break
        else:
            bad_count = 0

    print()

    if result_bad:
        print(
            f"  RESULT: BAD "
            f"(CPU >= {threshold:.1f}% for "
            f"{consecutive_required} consecutive samples)"
        )
    else:
        print(
            f"  RESULT: GOOD "
            f"(no sustained CPU >= "
            f"{threshold:.1f}%)"
        )

    print("  terminating Okular...")

    kill_process(target)

    # The launcher may itself still be alive.
    try:
        launcher.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            launcher.kill()
        except ProcessLookupError:
            pass

    return result_bad


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark EPUBs using Okular CPU usage."
    )

    parser.add_argument(
        "epubs",
        nargs="+",
    )

    parser.add_argument(
        "--warmup",
        type=float,
        # default=5.0, # Give Okular 5 seconds to load it.
        default=3.0, # Give Okular 3 seconds to load it.
    )

    parser.add_argument(
        "--duration",
        type=float,
        # default=5.0, # Then monitor for another 5 seconds.
        default=2.0, # Then monitor for another 2 seconds.
    )

    # A file is BAD only if it continues using ≥80% CPU for 3 consecutive 0.5-second samples.

    parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
    )

    parser.add_argument(
        "--threshold",
        type=float,
        default=80.0,
    )

    parser.add_argument(
        "--consecutive",
        type=int,
        default=3,
    )

    args = parser.parse_args()

    bad = 0

    for i, epub in enumerate(args.epubs, 1):
        print(f"\n[{i}/{len(args.epubs)}]")

        if benchmark(
            epub,
            warmup=args.warmup,
            duration=args.duration,
            interval=args.interval,
            threshold=args.threshold,
            consecutive_required=args.consecutive,
        ):
            bad += 1

    print()
    print("=" * 100)
    print(
        f"Finished: {bad} BAD / "
        f"{len(args.epubs)} total"
    )
    print("=" * 100)


if __name__ == "__main__":
    main()
