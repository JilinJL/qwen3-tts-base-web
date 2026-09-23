"""Owned subprocesses with streaming logs and bounded shutdown."""

import os
import re
import signal
import subprocess
import threading

import psutil

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class Cancelled(RuntimeError):
    pass


def spawn(command, **kwargs):
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    return subprocess.Popen([str(part) for part in command], **kwargs, **options)


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.Error:
        children = []
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=8)
    finally:
        # Nested CLI runners may have their own sessions; stop captured descendants too.
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(children, timeout=3)
        for child in alive:
            try:
                child.kill()
            except psutil.Error:
                pass


class Runner:
    def __init__(self, log=print):
        self.log = log
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self.process = None

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            process = self.process
        stop_process(process)

    def run(self, command, env=None, cwd=None):
        if self.cancelled.is_set():
            raise Cancelled("任务已取消")
        self.log("$ " + " ".join(str(part) for part in command))
        with self._lock:
            if self.cancelled.is_set():
                raise Cancelled("任务已取消")
            process = spawn(
                command,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.process = process
        try:
            for line in process.stdout:
                line = ANSI_ESCAPE.sub("", line).strip()
                if line:
                    self.log(line)
            code = process.wait()
            if self.cancelled.is_set():
                raise Cancelled("任务已取消，已下载文件保留以供续传")
            if code:
                raise RuntimeError(f"命令失败，退出码 {code}")
            return code
        finally:
            stop_process(process)
            process.stdout.close()
            with self._lock:
                self.process = None
