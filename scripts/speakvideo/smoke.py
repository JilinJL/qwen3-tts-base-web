"""Run against the installed Windows base agent, with explicitly fake inference."""

import asyncio
import base64
import hashlib
import io
import tempfile
import wave
from pathlib import Path

if __package__:
    from .base_agent import AgentSettings
    from .speakvideo_agent import SpeakVideoAgent
else:
    from base_agent import AgentSettings
    from speakvideo_agent import SpeakVideoAgent


class FakeQwen:
    calls = 0

    async def synthesize(self, job):
        self.calls += 1
        with io.BytesIO() as stream:
            with wave.open(stream, "wb") as writer:
                writer.setparams((1, 2, 24000, 0, "NONE", "none"))
                writer.writeframes(b"\x00\x00" * 240)
            return stream.getvalue(), 24000

    async def close(self):
        pass


async def verify(root):
    data = b"fixture-prompt"
    voice = root / "pt" / "fixture" / "fixture_calm.pt"
    voice.parent.mkdir(parents=True)
    voice.write_bytes(data)
    qwen = FakeQwen()
    settings = AgentSettings(
        root=root, gateway_url="wss://example.invalid", token="test", worker_id="LINHP"
    )
    agent = SpeakVideoAgent(settings, qwen)
    job = dict(
        job_id="j_" + "a" * 64,
        text="test",
        role="fixture",
        emotion="calm",
        language="Chinese",
        voice_sha256=hashlib.sha256(data).hexdigest(),
        generation_revision="test",
    )
    await agent.accept_job({"job": job, "request_id": "first-request"})
    assert agent._outbox[-1]["request_id"] == "first-request"
    await agent.accept_job({"job": job})
    task = asyncio.create_task(agent.job_worker())
    await asyncio.wait_for(agent.jobs.join(), timeout=2)
    await agent.accept_job({"job": job})
    assert qwen.calls == 1
    await agent.accept_job(
        {"job": {**job, "text": "different"}, "request_id": "conflicting-request"}
    )
    assert agent._outbox[-1]["type"] == "job.rejected"
    assert agent._outbox[-1]["request_id"] == "conflicting-request"
    assert base64.b64decode(agent._completed[job["job_id"]]["audio_base64"]).startswith(b"RIFF")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    agent.journal.close()
    replacement = SpeakVideoAgent(settings, qwen)
    assert replacement.journal.get(job["job_id"])["status"] == "failed"
    await replacement.accept_job({"job": job})
    assert replacement.jobs.qsize() == 0
    replacement.journal.close()
    print("Worker simulated inference smoke passed: dedupe, conflict, WAV, restart result loss")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(verify(Path(directory)))
