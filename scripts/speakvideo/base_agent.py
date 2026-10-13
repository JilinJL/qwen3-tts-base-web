"""Windows GPU worker for the SpeakVideo reverse-tunnel protocol.

The agent owns no model. It serializes jobs, calls the local qwen3-tts-web
HTTP service, and sends status plus in-memory WAV data over an outbound WSS
connection. The gateway can therefore be replaced without changing inference.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import inspect
import io
import json
import os
import re
import signal
import socket
import tempfile
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import httpx


# Vendored protocol core from the user's Windows Qwen worker (MIT).
# See upstream-license.txt. Inference remains in the local Qwen HTTP service.
def project_root(root=None):
    return Path(root or os.environ.get("QWEN3_TTS_ROOT", Path.cwd())).expanduser().resolve()


PROTOCOL_VERSION = "1"
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_PT_FILE_BYTES = 128 * 1024 * 1024
MAX_AUDIO_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class AgentSettings:
    root: Path
    gateway_url: str = ""
    token: str = ""
    worker_id: str = ""
    qwen_url: str = "http://127.0.0.1:8001"
    reconnect_min: float = 1.0
    reconnect_max: float = 30.0
    request_timeout: float = 3600.0
    max_queue: int = 32

    def validate(self) -> None:
        if not self.gateway_url.startswith(("ws://", "wss://")):
            raise ValueError("tts_agent.gateway_url must use ws:// or wss://")
        if not self.qwen_url.startswith(("http://", "https://")):
            raise ValueError("tts_agent.qwen_url must use http:// or https://")
        if not self.worker_id:
            raise ValueError("tts_agent.worker_id cannot be empty")
        if self.reconnect_min <= 0 or self.reconnect_max < self.reconnect_min:
            raise ValueError("invalid reconnect interval")
        if self.request_timeout <= 0 or self.max_queue < 1:
            raise ValueError("invalid agent limits")


def load_agent_settings(root: Path | None = None, overrides: dict[str, Any] | None = None):
    import tomllib

    root = project_root(root)
    values: dict[str, Any] = {
        "reconnect_min": 1.0,
        "reconnect_max": 30.0,
        "request_timeout": 3600.0,
        "max_queue": 32,
    }
    config_path = root / "config.toml"
    if config_path.is_file():
        with config_path.open("rb") as handle:
            values.update(tomllib.load(handle).get("tts_agent", {}))
    for key in (
        "gateway_url",
        "token",
        "worker_id",
        "qwen_url",
        "reconnect_min",
        "reconnect_max",
        "request_timeout",
        "max_queue",
    ):
        value = os.environ.get(f"QWEN3_TTS_AGENT_{key.upper()}")
        if value is not None:
            values[key] = value
    values.update({k: v for k, v in (overrides or {}).items() if v is not None})
    if not values.get("worker_id"):
        values["worker_id"] = socket.gethostname()
    for key in ("reconnect_min", "reconnect_max", "request_timeout"):
        values[key] = float(values[key])
    values["max_queue"] = int(values["max_queue"])
    return AgentSettings(root=root, **values)


def normalize_pt_path(value: str) -> Path:
    """Return a safe POSIX-relative path for a voice prompt file."""

    if not isinstance(value, str):
        raise ValueError("voice path must be a string")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or path.suffix.lower() != ".pt":
        raise ValueError("voice path must be a relative .pt path")
    if any(part in ("", ".", "..") or ":" in part for part in path.parts):
        raise ValueError("voice path contains an invalid component")
    if len(path.parts) < 2:
        raise ValueError("voice path must include a role directory")
    return Path(*path.parts)


def _validate_prompt_file(path: Path) -> None:
    import torch

    value = torch.load(str(path), map_location="cpu", weights_only=False)
    if isinstance(value, dict) and isinstance(value.get("items"), (list, tuple)):
        items = value["items"]
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        items = [value]
    if not items:
        raise ValueError("voice prompt is empty")
    item = items[0]
    fields = item.keys() if isinstance(item, dict) else vars(item).keys()
    expected = {"ref_code", "ref_spk_embedding", "x_vector_only_mode"}
    if not expected.intersection(fields):
        raise ValueError("voice prompt has no Qwen3-TTS prompt fields")


def sync_voice_files(root: Path, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and atomically install PT files received from the Mac source."""

    pt_root = (root / "pt").resolve()
    results = []
    for item in files:
        relative = normalize_pt_path(item.get("path", ""))
        encoded = item.get("content_base64") or item.get("data_base64")
        if not isinstance(encoded, str):
            raise ValueError(f"missing content for {relative.as_posix()}")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"invalid base64 for {relative.as_posix()}") from exc
        if not content or len(content) > MAX_PT_FILE_BYTES:
            raise ValueError(f"invalid PT size for {relative.as_posix()}")
        digest = hashlib.sha256(content).hexdigest()
        expected = str(item.get("sha256", "")).lower()
        if expected and expected != digest:
            raise ValueError(f"sha256 mismatch for {relative.as_posix()}")

        target = (pt_root / relative).resolve()
        if pt_root not in target.parents:
            raise ValueError("voice path escapes pt directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(content)
            _validate_prompt_file(temporary)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        results.append({"path": relative.as_posix(), "sha256": digest, "bytes": len(content)})
    return results


class QwenClient:
    def __init__(self, base_url: str, timeout: float):
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            trust_env=False,
            timeout=httpx.Timeout(timeout, connect=30.0),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def synthesize(self, job: dict[str, Any]) -> tuple[bytes, int]:
        payload = {
            "role": job.get("role", ""),
            "emotion": job.get("emotion", "平静"),
            "text": job["text"],
            "language": job.get("language", "Chinese"),
            "pt_file": job.get("pt_file", ""),
            "mode": "file",
            "persist": False,
        }
        response = await self.client.post("/api/tts", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(
                f"qwen3-tts-web returned {response.status_code}: {response.text[:1000]}"
            )
        if not response.headers.get("content-type", "").startswith("audio/"):
            raise RuntimeError("qwen3-tts-web did not return audio/wav")
        audio = response.content
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            raise RuntimeError("audio response exceeds the agent limit")
        sample_rate = int(response.headers.get("x-audio-sample-rate", "0") or 0)
        if not sample_rate:
            with wave.open(io.BytesIO(audio)) as handle:
                sample_rate = handle.getframerate()
        return audio, sample_rate


class Agent:
    def __init__(self, settings: AgentSettings, qwen: QwenClient | Any | None = None):
        self.settings = settings
        self.qwen = qwen or QwenClient(settings.qwen_url, settings.request_timeout)
        self.jobs: asyncio.Queue[dict[str, Any]] = asyncio.Queue(settings.max_queue)
        self._outbox: deque[dict[str, Any]] = deque()
        self._outbox_condition = asyncio.Condition()
        self._stop_event = asyncio.Event()
        self._known_jobs: set[str] = set()
        self._completed: dict[str, dict[str, Any]] = {}
        self._cancelled_jobs: set[str] = set()
        self._active_job_id: str | None = None
        self._active_task: asyncio.Task[Any] | None = None
        self._websocket: Any | None = None
        self._stopping = False

    async def emit(self, event: dict[str, Any]) -> None:
        async with self._outbox_condition:
            while len(self._outbox) >= self.settings.max_queue * 8 and not self._stopping:
                await self._outbox_condition.wait()
            if self._stopping:
                return
            self._outbox.append(event)
            self._outbox_condition.notify_all()

    async def next_event(self) -> dict[str, Any]:
        async with self._outbox_condition:
            while not self._outbox and not self._stopping:
                await self._outbox_condition.wait()
            if not self._outbox:
                raise asyncio.CancelledError
            event = self._outbox.popleft()
            self._outbox_condition.notify_all()
            return event

    async def requeue_event(self, event: dict[str, Any]) -> None:
        async with self._outbox_condition:
            self._outbox.appendleft(event)
            self._outbox_condition.notify_all()

    def remember_result(self, event: dict[str, Any]) -> None:
        job_id = event.get("job_id")
        if not job_id:
            return
        self._completed[job_id] = dict(event)
        while len(self._completed) > 256:
            self._completed.pop(next(iter(self._completed)))

    async def accept_job(self, message: dict[str, Any]) -> None:
        value = message.get("job") if isinstance(message.get("job"), dict) else message
        job_id = str(value.get("job_id") or message.get("job_id") or "")
        if not JOB_ID.fullmatch(job_id):
            await self.emit({"type": "job.rejected", "job_id": job_id, "error": "invalid job_id"})
            return
        if job_id in self._completed:
            await self.emit(dict(self._completed[job_id]))
            return
        if job_id in self._known_jobs:
            await self.emit({"type": "job.rejected", "job_id": job_id, "error": "duplicate job_id"})
            return
        text = value.get("text", "")
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            await self.emit({"type": "job.rejected", "job_id": job_id, "error": "invalid text"})
            return
        job = {
            "job_id": job_id,
            "text": text,
            "language": value.get("language", "Chinese"),
            "role": value.get("role", ""),
            "emotion": value.get("emotion", "平静"),
            "pt_file": value.get("pt_file", ""),
        }
        if not job["role"] and not job["pt_file"]:
            await self.emit(
                {"type": "job.rejected", "job_id": job_id, "error": "role or pt_file is required"}
            )
            return
        if job["pt_file"]:
            try:
                job["pt_file"] = normalize_pt_path(job["pt_file"]).as_posix()
            except ValueError as exc:
                await self.emit({"type": "job.rejected", "job_id": job_id, "error": str(exc)})
                return
        try:
            self.jobs.put_nowait(job)
        except asyncio.QueueFull:
            await self.emit({"type": "job.rejected", "job_id": job_id, "error": "queue is full"})
            return
        self._known_jobs.add(job_id)
        await self.emit(
            {"type": "job.accepted", "job_id": job_id, "queue_depth": self.jobs.qsize()}
        )

    async def cancel_job(self, message: dict[str, Any]) -> None:
        job_id = str(message.get("job_id", ""))
        if not job_id:
            return
        self._cancelled_jobs.add(job_id)
        if self._active_job_id == job_id and self._active_task is not None:
            self._active_task.cancel()

    async def sync_voices(self, message: dict[str, Any]) -> None:
        sync_id = str(message.get("sync_id", ""))
        files = message.get("files", [])
        try:
            if not isinstance(files, list) or not files:
                raise ValueError("files must be a non-empty list")
            result = await asyncio.to_thread(sync_voice_files, self.settings.root, files)
            await self.emit(
                {"type": "voice.sync.result", "sync_id": sync_id, "status": "ok", "files": result}
            )
        except Exception as exc:
            await self.emit(
                {
                    "type": "voice.sync.result",
                    "sync_id": sync_id,
                    "status": "failed",
                    "error": str(exc),
                }
            )

    async def handle_message(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "worker.ping":
            await self.emit(
                {"type": "worker.pong", "worker_id": self.settings.worker_id, "ts": time.time()}
            )
        elif kind == "job.submit":
            await self.accept_job(message)
        elif kind == "job.cancel":
            await self.cancel_job(message)
        elif kind == "voice.sync":
            await self.sync_voices(message)
        else:
            await self.emit({"type": "worker.error", "error": f"unknown message type: {kind}"})

    async def job_worker(self) -> None:
        while not self._stopping:
            job = await self.jobs.get()
            job_id = job["job_id"]
            self._active_job_id = job_id
            self._active_task = asyncio.current_task()
            try:
                if job_id in self._cancelled_jobs:
                    event = {"type": "job.result", "job_id": job_id, "status": "cancelled"}
                    self.remember_result(event)
                    await self.emit(event)
                    continue
                await self.emit({"type": "job.running", "job_id": job_id})
                audio, sample_rate = await self.qwen.synthesize(job)
                event = {
                    "type": "job.result",
                    "job_id": job_id,
                    "status": "succeeded",
                    "content_type": "audio/wav",
                    "sample_rate": sample_rate,
                    "bytes": len(audio),
                    "audio_base64": base64.b64encode(audio).decode("ascii"),
                }
                self.remember_result(event)
                await self.emit(event)
            except asyncio.CancelledError:
                event = {"type": "job.result", "job_id": job_id, "status": "cancelled"}
                self.remember_result(event)
                await self.emit(event)
            except Exception as exc:
                event = {
                    "type": "job.result",
                    "job_id": job_id,
                    "status": "failed",
                    "error": str(exc),
                }
                self.remember_result(event)
                await self.emit(event)
            finally:
                self.jobs.task_done()
                self._cancelled_jobs.discard(job_id)
                self._active_job_id = None
                self._active_task = None

    async def sender(self, websocket: Any) -> None:
        while not self._stopping:
            event = await self.next_event()
            try:
                await websocket.send(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            except Exception:
                await self.requeue_event(event)
                raise

    async def receiver(self, websocket: Any) -> None:
        async for raw in websocket:
            if isinstance(raw, bytes):
                await self.emit(
                    {"type": "worker.error", "error": "binary gateway messages are not supported"}
                )
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await self.emit({"type": "worker.error", "error": "invalid JSON message"})
                continue
            if not isinstance(message, dict):
                await self.emit({"type": "worker.error", "error": "message must be an object"})
                continue
            await self.handle_message(message)

    async def connect_once(self) -> None:
        import websockets

        headers = {"X-Worker-ID": self.settings.worker_id}
        if self.settings.token:
            headers["Authorization"] = f"Bearer {self.settings.token}"
        kwargs: dict[str, Any] = {
            "ping_interval": 20,
            "ping_timeout": 20,
            "max_size": MAX_PT_FILE_BYTES * 2,
        }
        parameter_name = "additional_headers"
        if "extra_headers" in inspect.signature(websockets.connect).parameters:
            parameter_name = "extra_headers"
        kwargs[parameter_name] = headers
        async with websockets.connect(self.settings.gateway_url, **kwargs) as websocket:
            self._websocket = websocket
            try:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "worker.hello",
                            "protocol": PROTOCOL_VERSION,
                            "worker_id": self.settings.worker_id,
                            "capabilities": {
                                "tts": True,
                                "voice_sync": True,
                                "audio_encoding": "base64-wav",
                            },
                        },
                        separators=(",", ":"),
                    )
                )
                sender = asyncio.create_task(self.sender(websocket))
                receiver = asyncio.create_task(self.receiver(websocket))
                done, pending = await asyncio.wait(
                    (sender, receiver), return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    exception = task.exception()
                    if exception:
                        raise exception
            finally:
                self._websocket = None

    async def run(self) -> None:
        self.settings.validate()
        worker = asyncio.create_task(self.job_worker())
        delay = self.settings.reconnect_min
        try:
            while not self._stopping:
                try:
                    print(
                        f"[agent] connecting worker={self.settings.worker_id} url={self.settings.gateway_url}",
                        flush=True,
                    )
                    await self.connect_once()
                    delay = self.settings.reconnect_min
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"[agent] disconnected: {exc}", flush=True)
                    if not self._stopping:
                        try:
                            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                        except asyncio.TimeoutError:
                            pass
                        delay = min(delay * 2, self.settings.reconnect_max)
        finally:
            self._stopping = True
            async with self._outbox_condition:
                self._outbox_condition.notify_all()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            close = getattr(self.qwen, "close", None)
            if close is not None:
                await close()

    def stop(self) -> None:
        self._stopping = True
        self._stop_event.set()
        if self._active_task is not None:
            self._active_task.cancel()
        if self._websocket is not None:
            try:
                asyncio.create_task(self._websocket.close())
            except RuntimeError:
                pass


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="SpeakVideo Windows GPU TTS agent")
    result.add_argument("--root", type=Path)
    result.add_argument("--gateway-url")
    result.add_argument("--token")
    result.add_argument("--worker-id")
    result.add_argument("--qwen-url")
    result.add_argument("--print-config", action="store_true")
    result.add_argument("--allow-anonymous", action="store_true")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    settings = load_agent_settings(
        args.root,
        {
            "gateway_url": args.gateway_url,
            "token": args.token,
            "worker_id": args.worker_id,
            "qwen_url": args.qwen_url,
        },
    )
    if args.print_config:
        safe = {
            **settings.__dict__,
            "root": str(settings.root),
            "token": "***" if settings.token else "",
        }
        print(json.dumps(safe, indent=2))
        return 0
    if not settings.token and not args.allow_anonymous:
        raise SystemExit(
            "tts-agent requires a token; use --allow-anonymous only for local development"
        )
    agent = Agent(settings)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, agent.stop)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        loop.run_until_complete(agent.run())
    except KeyboardInterrupt:
        agent.stop()
        return 130
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
