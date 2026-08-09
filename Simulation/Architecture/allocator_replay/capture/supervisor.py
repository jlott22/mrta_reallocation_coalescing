from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from allocator_replay.config.study import TRACE_ROOT


def _wait_for_process(process_id: int) -> None:
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        infinite = 0xFFFFFFFF
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(synchronize, False, process_id)
        if not handle:
            return
        try:
            kernel32.WaitForSingleObject(handle, infinite)
        finally:
            kernel32.CloseHandle(handle)
        return
    while True:
        try:
            os.kill(process_id, 0)
        except OSError:
            return
        time.sleep(15.0)


def _write_status(value: dict[str, object]) -> None:
    path = TRACE_ROOT / "capture_supervisor.json"
    temporary = path.with_suffix(".json.tmp")
    value["updated_at"] = datetime.now(timezone.utc).isoformat()
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def supervise(process_id: int, workers: int) -> int:
    _write_status(
        {
            "status": "waiting_for_primary_capture",
            "primary_pid": process_id,
            "workers": workers,
        }
    )
    _wait_for_process(process_id)
    _write_status(
        {
            "status": "resuming_and_sealing",
            "primary_pid": process_id,
            "workers": workers,
        }
    )
    stdout_path = TRACE_ROOT / "capture_finalize_stdout.log"
    stderr_path = TRACE_ROOT / "capture_finalize_stderr.log"
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        result = subprocess.run(
            [
                sys.executable,
                "-u",
                "-m",
                "allocator_replay",
                "capture",
                "--workers",
                str(workers),
            ],
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
    _write_status(
        {
            "status": "complete" if result.returncode == 0 else "failed",
            "primary_pid": process_id,
            "workers": workers,
            "returncode": result.returncode,
            "stdout": str(stdout_path.resolve()),
            "stderr": str(stderr_path.resolve()),
        }
    )
    return int(result.returncode)


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv or sys.argv[1:])
    if len(arguments) != 2:
        raise SystemExit("usage: supervisor.py PRIMARY_PID WORKERS")
    return supervise(int(arguments[0]), int(arguments[1]))


if __name__ == "__main__":
    raise SystemExit(main())
