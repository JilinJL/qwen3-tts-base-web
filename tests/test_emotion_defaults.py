"""Emotion defaults remain consistent between voice management and synthesis."""
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api(tmp_path, monkeypatch):
    from qwen3_tts_web import server

    monkeypatch.setattr(server, "PT_DIR", tmp_path / "pt")
    monkeypatch.setattr(server, "OUT_DIR", tmp_path / "output")
    server.PT_DIR.mkdir()
    server.OUT_DIR.mkdir()
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference")
    prompt = [SimpleNamespace(ref_spk_embedding=np.ones(2), ref_code=None)]
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return [np.ones(100, dtype=np.float32) * .1], 24000

    monkeypatch.setattr(server, "model", SimpleNamespace(
        create_voice_clone_prompt=lambda **kwargs: prompt,
        generate_voice_clone=generate,
    ))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        Tensor=np.ndarray,
        save=lambda obj, path: Path(path).write_bytes(b"prompt"),
        load=lambda *args, **kwargs: prompt,
    ))
    # No lifespan: tests must never load real weights or change the user's model cache.
    client = TestClient(server.app)
    try:
        yield client, server, reference, calls
    finally:
        client.close()


@pytest.mark.parametrize("extra", [{}, {"emotion": ""}, {"emotion": "  \t"}, {"emotion": None}])
def test_create_and_generate_with_default(api, extra):
    client, server, reference, calls = api
    result = client.post("/api/pt/create", json={"role": "Test", "ref_audio": str(reference), **extra})
    assert result.status_code == 200, result.text
    assert result.json()["emotion"] == "平静"
    assert (server.PT_DIR / "Test/Test_平静.pt").is_file()
    response = client.post("/api/tts", json={"role": "Test", "text": "你好", **extra})
    assert response.status_code == 200, response.text
    assert response.json()["emotion"] == "平静"
    assert len(calls) == 1


@pytest.mark.parametrize("query", [{}, {"emotion": ""}, {"emotion": "  "}])
def test_get_endpoints_default_to_calm(api, query):
    client, _, reference, _ = api
    client.post("/api/pt/create", json={"role": "Test", "ref_audio": str(reference)})
    for endpoint in ("inspect", "download"):
        result = client.get(f"/api/pt/{endpoint}", params={"role": "Test", **query})
        assert result.status_code == 200, result.text


def test_explicit_emotion_and_missing_calm(api):
    client, _, reference, calls = api
    result = client.post("/api/pt/create", json={"role": "Test", "ref_audio": str(reference), "emotion": " 愤怒 "})
    assert result.status_code == 200
    assert result.json()["emotion"] == "愤怒"
    assert client.post("/api/tts", json={"role": "Test", "text": "你好"}).status_code == 404
    assert not calls
    result = client.post("/api/tts", json={"role": "Test", "emotion": "愤怒", "text": "你好"})
    assert result.status_code == 200
    assert result.json()["emotion"] == "愤怒"


def test_invalid_emotion_remains_rejected(api):
    client, _, reference, _ = api
    response = client.post("/api/pt/create", json={"role": "Test", "ref_audio": str(reference), "emotion": "../bad"})
    assert response.status_code == 400
    response = client.post("/api/pt/create", json={"role": "Test", "ref_audio": str(reference), "emotion": 123})
    assert response.status_code == 422


def test_schema_marks_emotion_optional(api):
    schemas = api[0].get("/openapi.json").json()["components"]["schemas"]
    for name in ("PromptCreateRequest", "TTSRequest"):
        assert "emotion" not in schemas[name]["required"]
        assert schemas[name]["properties"]["emotion"]["default"] == "平静"
