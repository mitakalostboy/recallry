"""Bounded POSIX subprocesses with process-group cleanup on timeout or parent exit."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

GRACE_SECONDS = 3.0
# $1 = parent pid, $2 = child process group. Exits as soon as the group is gone.
WATCHDOG = ('while kill -0 "$1" 2>/dev/null && kill -0 -"$2" 2>/dev/null; do sleep 1; done; '
            'kill -0 -"$2" 2>/dev/null || exit 0; kill -TERM -"$2" 2>/dev/null; sleep 3; '
            'kill -KILL -"$2" 2>/dev/null; exit 0')


@dataclass(frozen=True)
class RunResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    cancelled: bool
    elapsed: float
    output_limited: bool = False


class Busy(RuntimeError):
    pass


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_group(process: subprocess.Popen, grace: float = GRACE_SECONDS) -> None:
    """SIGTERM the child's process group, then SIGKILL survivors."""
    pgid = process.pid  # start_new_session=True makes pid == pgid
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if process.poll() is not None and not _group_alive(pgid):
            return
        time.sleep(0.05)
    _signal_group(pgid, signal.SIGKILL)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:  # pragma: no cover - kernel did not reap
        pass


class ChildSupervisor:
    """Tracks owned subprocesses and their watchdogs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._children: dict[int, subprocess.Popen] = {}
        self._watchdogs: dict[int, subprocess.Popen] = {}
        self._closed = False

    def spawn(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path,
              stdin: int | None = subprocess.DEVNULL) -> subprocess.Popen:
        if not argv or not os.path.isabs(argv[0]):
            raise ValueError("child executable must be an absolute path")
        with self._lock:
            if self._closed:
                raise RuntimeError("supervisor is shut down")
            process = subprocess.Popen(
                list(argv), shell=False, cwd=str(cwd), env=dict(env), stdin=stdin,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, close_fds=True,
            )
            self._children[process.pid] = process
            self._watchdogs[process.pid] = subprocess.Popen(
                ["/bin/sh", "-c", WATCHDOG, "recallry-watchdog", str(os.getpid()), str(process.pid)],
                shell=False, env={"PATH": "/usr/bin:/bin"}, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True)
        return process

    def forget(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._children.pop(process.pid, None)
            watchdog = self._watchdogs.pop(process.pid, None)
        if watchdog is not None:
            stop_group(watchdog, 0.5)

    def pids(self) -> list[int]:
        with self._lock:
            return sorted(self._children)

    def stop_all(self, grace: float = GRACE_SECONDS) -> list[int]:
        """Stop every owned child (idempotent); returns the pids that were signalled."""
        with self._lock:
            self._closed = True
            children = list(self._children.values())
            self._children.clear()
            watchdogs = list(self._watchdogs.values())
            self._watchdogs.clear()
        for process in children:
            stop_group(process, grace)
        for watchdog in watchdogs:  # their groups are gone; do not wait for the next poll
            stop_group(watchdog, 0.5)
        return [process.pid for process in children]


class BoundedRunner:
    """One child at a time, stdin payload, wall-clock timeout and cancel."""

    def __init__(self, supervisor: ChildSupervisor) -> None:
        self.supervisor = supervisor
        self._busy = threading.Lock()
        self._cancel = threading.Event()
        self._current: subprocess.Popen | None = None

    def cancel(self) -> bool:
        self._cancel.set()
        process = self._current
        if process is not None and process.poll() is None:
            stop_group(process)
            return True
        return False

    def run(self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path,
            stdin_data: bytes, timeout: float, max_output: int = 1024 * 1024, launch_guard=nullcontext) -> RunResult:
        if not self._busy.acquire(blocking=False):
            raise Busy("another AI request is already running")
        try:
            self._cancel.clear()
            started = time.monotonic()
            # Serialize the final unattended consent check with process creation only.
            with launch_guard():
                process = self.supervisor.spawn(argv, env=env, cwd=cwd, stdin=subprocess.PIPE)
            self._current = process
            try:
                outcome: dict[str, bytes] = {}

                limited = threading.Event()

                def read_pipe(pipe, key):
                    chunks, size = [], 0
                    try:
                        while chunk := pipe.read(8192):
                            room = max_output - size
                            chunks.append(chunk[:room])
                            size += len(chunk)
                            if size > max_output:
                                limited.set()
                                break
                    finally:
                        outcome[key] = b"".join(chunks)
                        pipe.close()

                def write_input():
                    try:
                        process.stdin.write(stdin_data)
                        process.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass
                    finally:
                        process.stdin.close()

                workers = [threading.Thread(target=read_pipe, args=(process.stdout, "out"), daemon=True),
                           threading.Thread(target=read_pipe, args=(process.stderr, "err"), daemon=True),
                           threading.Thread(target=write_input, daemon=True)]
                for worker in workers:
                    worker.start()
                timed_out = False
                deadline = started + timeout
                while any(worker.is_alive() for worker in workers) or process.poll() is None:
                    if self._cancel.is_set() or limited.is_set():
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    time.sleep(0.05)
                if any(worker.is_alive() for worker in workers) or process.poll() is None:
                    stop_group(process)
                    for worker in workers:
                        worker.join(GRACE_SECONDS * 2)
                elif _group_alive(process.pid):
                    # The CLI exited but left grandchildren behind: never orphan them.
                    stop_group(process)
                return RunResult(
                    process.returncode, outcome.get("out", b""), outcome.get("err", b""),
                    timed_out, self._cancel.is_set() and not timed_out,
                    time.monotonic() - started,
                    limited.is_set(),
                )
            finally:
                self._current = None
                self.supervisor.forget(process)
        finally:
            self._busy.release()
