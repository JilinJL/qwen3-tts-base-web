"""Durable extension for the bundled Windows WSS protocol core.

Copy worker files to E:/qwen3-tts-base-web/scripts/speakvideo/.
Uses the existing inference environment; loads no GPU model itself.
The matching protocol core is included as base_agent.py.
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
import hashlib
import json
import re
from pathlib import Path

import httpx
from filelock import FileLock

if __package__:
    from .base_agent import Agent, QwenClient, load_agent_settings
    from .journal import Journal
    from .metrics import collect_metrics
    from .interface_docs import build_interface_document
    from .voice_library import VoiceLibrary
else:
    from base_agent import Agent, QwenClient, load_agent_settings
    from journal import Journal
    from metrics import collect_metrics
    from interface_docs import build_interface_document
    from voice_library import VoiceLibrary

NAME = re.compile(r"^[\w-]{1,64}$")
ID = re.compile(r"^j_[a-f0-9]{64}$")
SHA = re.compile(r"^[a-f0-9]{64}$")
MAX_CACHED_BASE64 = 128 * 1024 * 1024


def voice_path(root: Path, role: str, emotion: str) -> Path:
    if not NAME.fullmatch(role) or not NAME.fullmatch(emotion):
        raise ValueError("invalid voice name")
    path = (root / "pt" / role / f"{role}_{emotion}.pt").resolve()
    if (root / "pt").resolve() not in path.parents:
        raise ValueError("voice path escapes PT root")
    return path


class VerifiedQwen:
    def __init__(self, agent, client):
        self.agent, self.client = agent, client

    async def close(self):
        await self.client.close()

    async def synthesize(self, job):
        async with self.agent.asset_lock:
            row = self.agent.journal.get(job["job_id"])
            payload = row["payload"]
            path = voice_path(self.agent.settings.root, payload["role"], payload["emotion"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != payload["voice_sha256"]:
                raise ValueError("voice_sha256 mismatch; sync the Mac source before retrying")
            return await self.client.synthesize(job)


class SpeakVideoAgent(Agent):
    def __init__(self, settings, qwen=None):
        super().__init__(settings, qwen or QwenClient(settings.qwen_url, settings.request_timeout))
        self.journal = Journal(settings.root / ".speakvideo" / "jobs.sqlite3")
        self.asset_lock = asyncio.Lock()
        self.voice_library = VoiceLibrary(settings.root)
        self.event_log = deque(maxlen=50)
        self.rpc_tasks = set()
        self.qwen = VerifiedQwen(self, self.qwen)

    async def emit(self, event):
        if event.get("type", "").startswith(("job.", "voice.", "worker.")):
            summary = event.get("type", "event")
            if event.get("job_id"):
                summary += f" job_id={event['job_id']}"
            if event.get("status"):
                summary += f" status={event['status']}"
            if event.get("error"):
                summary += f" error={str(event['error'])[:200]}"
            self.event_log.append(summary)
        if event.get("type") == "job.running":
            self.journal.update(event["job_id"], "running")
        await super().emit(event)

    async def accept_job(self, message):
        value = message.get("job", {})
        job_id = value.get("job_id", "")
        request_id = message.get("request_id", "")
        try:
            if not ID.fullmatch(job_id):
                raise ValueError("invalid job ID")
            payload = {
                key: value.get(key, "")
                for key in (
                    "job_id",
                    "text",
                    "role",
                    "emotion",
                    "language",
                    "voice_sha256",
                    "generation_revision",
                )
            }
            if not isinstance(payload["text"], str) or not payload["text"].strip():
                raise ValueError("text required")
            if len(payload["text"]) > 10000 or not SHA.fullmatch(payload["voice_sha256"]):
                raise ValueError("invalid text or voice SHA256")
            path = voice_path(self.settings.root, payload["role"], payload["emotion"])
            if not path.is_file():
                raise ValueError("voice asset not found")
            previous = self.journal.get(job_id)
            if previous:
                if previous["digest"] != self.journal.digest(payload):
                    raise ValueError("idempotency key reused with different parameters")
                # Repeated submission never queues a second inference, even after a restart.
                await self.emit(
                    {"type": "job.accepted", "job_id": job_id, "request_id": request_id}
                )
                return
            if self.jobs.full():
                raise ValueError("queue is full")
            self.journal.insert(payload)  # fsync BEFORE acknowledging acceptance.
            self.jobs.put_nowait(payload)
            self._known_jobs.add(job_id)
            await self.emit({"type": "job.accepted", "job_id": job_id, "request_id": request_id})
        except Exception as exc:
            await self.emit(
                {
                    "type": "job.rejected",
                    "job_id": job_id,
                    "request_id": request_id,
                    "error": str(exc),
                }
            )

    def remember_result(self, event):
        job_id = event["job_id"]
        if event.get("status") == "succeeded":
            audio_size = len(event.get("audio_base64", ""))
            if audio_size > MAX_CACHED_BASE64:
                event.pop("audio_base64", None)
                event["status"] = "failed"
                event["error"] = "audio_cache_evicted; explicitly retry"
            else:
                self._completed[job_id] = dict(event)
        else:
            self._completed[job_id] = dict(event)
        self._known_jobs.discard(job_id)
        while (
            len(self._completed) > 256
            or sum(len(v.get("audio_base64", "")) for v in self._completed.values())
            > MAX_CACHED_BASE64
        ):
            oldest = next(iter(self._completed))
            removed = self._completed.pop(oldest)
            self.journal.update(oldest, "failed", "audio_cache_evicted; explicitly retry")
            if oldest == job_id:
                event.clear()
                event.update(removed)
                event["status"] = "failed"
                event.pop("audio_base64", None)
                event["error"] = "audio_cache_evicted; explicitly retry"
        self.journal.update(job_id, event["status"], event.get("error", ""))

    async def sync_voices(self, message):
        async with self.asset_lock:
            await super().sync_voices(message)

    async def rpc(self, message):
        kind = message["type"]
        response = {"type": "rpc.result", "request_id": message.get("request_id", "")}
        try:
            if kind == "worker.capabilities":
                response["result"] = build_interface_document(self.settings.worker_id)
            elif kind == "job.query":
                job_id = message.get("job_id", "")
                row = self.journal.get(job_id)
                response.update(job_id=job_id, status="missing" if row is None else row["status"])
                if row:
                    response["error"] = row["error"]
                    if message.get("include_audio") and row["status"] == "succeeded":
                        response.update(self._completed[job_id])
                        # Keep the RPC correlation instead of treating this as unsolicited job.result.
                        response["type"] = "rpc.result"
            elif kind == "voice.download":
                path = voice_path(self.settings.root, message["role"], message["emotion"])
                data = path.read_bytes()
                if not data or len(data) > 32 * 1024 * 1024:
                    raise ValueError("PT file exceeds gateway limit")
                response["content_base64"] = base64.b64encode(data).decode("ascii")
                response["sha256"] = hashlib.sha256(data).hexdigest()
            else:
                async with httpx.AsyncClient(
                    base_url=self.settings.qwen_url,
                    trust_env=False,
                    timeout=self.settings.request_timeout
                    if kind in {"voice.create", "voice.design"}
                    else 10,
                ) as client:
                    if kind == "worker.health":
                        result = await client.get("/api/health")
                        result.raise_for_status()
                        response.update(
                            result.json(),
                            worker_id=self.settings.worker_id,
                            capabilities={
                                "voice_manage": True,
                                "voice_permanent_delete": True,
                                "voice_reference": True,
                                "metrics": True,
                                "interface_docs": True,
                            },
                        )
                        if message.get("include_docs", True):
                            response["interface_docs"] = build_interface_document(
                                self.settings.worker_id
                            )
                    elif kind == "worker.metrics":
                        response["result"] = await asyncio.to_thread(
                            collect_metrics,
                            queue_depth=self.jobs.qsize(),
                            current_job_id=self._active_job_id,
                            logs=self.event_log,
                        )
                    elif kind == "voice.list":
                        result = await client.get("/api/pt/list")
                        result.raise_for_status()
                        response["result"] = result.json()
                    elif kind == "voice.create":
                        metadata = message.get("metadata")
                        if not isinstance(metadata, dict):
                            raise ValueError("metadata must be an object")
                        content_base64 = message.get("content_base64", "")
                        async with self.asset_lock:
                            self.voice_library.prepare_create(metadata, content_base64)
                            payload = dict(
                                metadata,
                                ref_audio="",
                                ref_audio_base64=content_base64,
                            )
                            result = await client.post("/api/pt/create", json=payload)
                            if result.is_error:
                                raise ValueError(result.text[:1000])
                            response["result"] = self.voice_library.record_created(
                                metadata, content_base64, result=result.json()
                            )
                    elif kind == "voice.design":
                        payload = dict(
                            message["payload"], persist=False, mode="file", allow_download=False
                        )
                        async with self.asset_lock:
                            result = await client.post("/api/voice-design", json=payload)
                            if result.is_error:
                                raise ValueError(result.text[:1000])
                            if len(result.content) > 64 * 1024 * 1024:
                                raise ValueError("design WAV exceeds worker limit")
                            response["audio_base64"] = base64.b64encode(result.content).decode()
                    elif kind == "voice.delete":
                        async with self.asset_lock:
                            response["result"] = await asyncio.to_thread(
                                self.voice_library.delete, message
                            )
                    elif kind == "voice.manage":
                        payload = message.get("payload")
                        if not isinstance(payload, dict):
                            raise ValueError("payload must be an object")
                        async with self.asset_lock:
                            response["result"] = await asyncio.to_thread(
                                self.voice_library.manage, payload
                            )
                    else:
                        raise ValueError("unknown RPC type")
        except Exception as exc:
            # Job failures are valid query results; RPC transport errors use rpc_error.
            response["rpc_error"] = str(exc)
        await self.emit(response)

    async def handle_message(self, message):
        if message.get("type") in {
            "worker.health",
            "worker.capabilities",
            "worker.metrics",
            "job.query",
            "voice.list",
            "voice.download",
            "voice.create",
            "voice.design",
            "voice.delete",
            "voice.manage",
        }:
            # Slow GPU operations must not block receiving health/status requests.
            task = asyncio.create_task(self.rpc(message))
            self.rpc_tasks.add(task)
            task.add_done_callback(self.rpc_tasks.discard)
        else:
            await super().handle_message(message)

    async def connect_once(self):
        import inspect

        import websockets

        headers = {
            "Authorization": f"Bearer {self.settings.token}",
            "X-Worker-ID": self.settings.worker_id,
        }
        header_key = (
            "extra_headers"
            if "extra_headers" in inspect.signature(websockets.connect).parameters
            else "additional_headers"
        )
        async with websockets.connect(
            self.settings.gateway_url,
            **{header_key: headers},
            ping_interval=20,
            ping_timeout=20,
            max_size=96 * 1024 * 1024,
        ) as socket:
            self._websocket = socket
            await socket.send(
                json.dumps(
                    {
                        "type": "worker.hello",
                        "protocol": "1",
                        "worker_id": self.settings.worker_id,
                        "capabilities": {
                            "tts": True,
                            "voice_sync": True,
                            "audio_encoding": "base64-wav",
                            "durable_jobs": True,
                            "rpc": True,
                            "correlated_acks": True,
                            "interface_docs": True,
                            "voice_permanent_delete": True,
                        },
                    }
                )
            )
            try:
                sender = asyncio.create_task(self.sender(socket))
                receiver = asyncio.create_task(self.receiver(socket))
                done, pending = await asyncio.wait(
                    (sender, receiver), return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    task.result()
            finally:
                self._websocket = None

    async def run(self):
        self.rpc_tasks = set()
        try:
            await super().run()
        finally:
            for task in list(self.rpc_tasks):
                task.cancel()
            await asyncio.gather(*self.rpc_tasks, return_exceptions=True)
            self.journal.close()


def main():
    settings = load_agent_settings()
    settings.validate()
    if not settings.gateway_url.startswith("wss://") or not settings.token:
        raise SystemExit("configured WSS URL and worker token required")
    directory = settings.root / ".speakvideo"
    directory.mkdir(exist_ok=True)
    with FileLock(directory / "agent.lock", timeout=0):
        asyncio.run(SpeakVideoAgent(settings).run())


if __name__ == "__main__":
    main()
