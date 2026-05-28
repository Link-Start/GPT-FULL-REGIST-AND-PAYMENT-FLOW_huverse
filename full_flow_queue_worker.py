#!/usr/bin/env python3
"""Queue/concurrency runner for the full-flow orchestrator.

This wrapper keeps the main single-run flow intact and lets operators drain a
SQLite-backed resource queue with one or more workers.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
MAIN_SCRIPT = ROOT / "trial_payment_full_flow.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the full-flow orchestrator in queue/worker mode.")
    parser.add_argument("--pool-db", default=os.environ.get("FULL_FLOW_POOL_DB", str(ROOT / "accfile" / "pool" / "full_flow.sqlite3")))
    parser.add_argument("--workers", type=int, default=1, help="Number of concurrent worker processes.")
    parser.add_argument("--max-runs", type=int, default=0, help="Stop after starting this many child runs. Default: unlimited.")
    parser.add_argument("--success-target", type=int, default=0, help="Stop after this many successful child runs. Default: disabled.")
    parser.add_argument("--worker-prefix", default="queue")
    parser.add_argument("--main-script", default=str(MAIN_SCRIPT))
    parser.add_argument("--python", default=sys.executable)
    return parser


def strip_option(args: list[str], option: str) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for item in args:
        if skip_next:
            skip_next = False
            continue
        if item == option:
            skip_next = True
            continue
        cleaned.append(item)
    return cleaned


def stream_process(cmd: list[str], *, worker_label: str, stop_event: threading.Event, print_lock: threading.Lock) -> tuple[int, str]:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    captured: list[str] = []
    for line in proc.stdout:
        captured.append(line)
        with print_lock:
            print(f"[{worker_label}] {line}", end="", flush=True)
        if "pool is empty" in line:
            stop_event.set()
    rc = proc.wait()
    return rc, "".join(captured)


def summary_status_for(run_id: str) -> str:
    summary_path = ROOT / "runtime" / "full_flow" / run_id / "summary.json"
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(data.get("status") or "")


def is_successful_child(rc: int, run_id: str) -> bool:
    if rc != 0:
        return False
    status = summary_status_for(run_id)
    return not status or status.startswith("success")


def worker_loop(
    *,
    index: int,
    worker_prefix: str,
    base_cmd: list[str],
    max_runs: int,
    success_target: int,
    run_state: dict[str, int],
    run_lock: threading.Lock,
    stop_event: threading.Event,
    print_lock: threading.Lock,
) -> None:
    worker_label = f"{worker_prefix}{index + 1}"
    worker_id = f"{worker_label}"
    while not stop_event.is_set():
        with run_lock:
            if success_target > 0 and run_state.get("success", 0) >= success_target:
                stop_event.set()
                return
            if success_target > 0 and run_state.get("success", 0) + run_state.get("running", 0) >= success_target:
                return
            if max_runs > 0 and run_state["started"] >= max_runs:
                stop_event.set()
                return
            run_state["started"] += 1
            run_state["running"] = run_state.get("running", 0) + 1
            child_index = run_state["started"]
        child_run_id = f"{worker_label}_{child_index:04d}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        cmd = base_cmd + ["--pool-worker-id", worker_id, "--run-id", child_run_id]
        with print_lock:
            print(f"[queue] child_start worker={worker_label} runId={child_run_id}", flush=True)
            print(f"[{worker_label}] start {shlex.join(cmd)}", flush=True)
        rc, output = stream_process(cmd, worker_label=worker_label, stop_event=stop_event, print_lock=print_lock)
        success = is_successful_child(rc, child_run_id)
        with run_lock:
            run_state["running"] = max(0, run_state.get("running", 0) - 1)
            if success:
                run_state["success"] = run_state.get("success", 0) + 1
            success_count = run_state.get("success", 0)
        with print_lock:
            print(f"[queue] child_done worker={worker_label} runId={child_run_id} rc={rc} success={int(success)} totalSuccess={success_count}", flush=True)
        if success_target > 0 and success_count >= success_target:
            stop_event.set()
            return
        if "pool is empty" in output:
            stop_event.set()
            return
        if rc == 0:
            continue
        lower = output.lower()
        if rc == 2 and "usage:" in lower:
            stop_event.set()
            return


def main() -> int:
    parser = build_parser()
    args, passthrough = parser.parse_known_args()
    passthrough = strip_option(strip_option(passthrough, "--pool-db"), "--pool-worker-id")
    passthrough = strip_option(passthrough, "--run-id")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.max_runs < 0:
        parser.error("--max-runs must be >= 0")
    if args.success_target < 0:
        parser.error("--success-target must be >= 0")
    main_script = Path(args.main_script).resolve()
    if not main_script.exists():
        parser.error(f"main script not found: {main_script}")

    base_cmd = [
        args.python,
        str(main_script),
        "--pool-db",
        str(Path(args.pool_db).expanduser().resolve()),
    ] + passthrough

    stop_event = threading.Event()
    print_lock = threading.Lock()
    run_lock = threading.Lock()
    run_state = {"started": 0, "running": 0, "success": 0}
    threads: list[threading.Thread] = []
    for index in range(args.workers):
        thread = threading.Thread(
            target=worker_loop,
            kwargs={
                "index": index,
                "worker_prefix": args.worker_prefix,
                "base_cmd": base_cmd,
                "max_runs": int(args.max_runs),
                "success_target": int(args.success_target),
                "run_state": run_state,
                "run_lock": run_lock,
                "stop_event": stop_event,
                "print_lock": print_lock,
            },
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()
    success = int(run_state.get("success", 0))
    started = int(run_state.get("started", 0))
    running = int(run_state.get("running", 0))
    print(
        f"[queue] finished started={started} success={success} running={running} "
        f"target={int(args.success_target)}",
        flush=True,
    )
    if args.success_target > 0 and success < args.success_target:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
