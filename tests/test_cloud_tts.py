"""Cloud TTS request/WAV contracts and both clients, without network or model loads."""

import base64
import io
import json
import time
import urllib.error
import wave

import pytest
from fastapi.testclient import TestClient

from mdspeak import engine, server, speak


@pytest.fixture(params=["gemini", "cloudflare"])
def cloud(request, monkeypatch, tmp_path):
    provider = request.param
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x20" * 2400)
    data = buffer.getvalue()
    calls = []

    def respond(request, timeout):
        assert timeout == 120
        payload = json.loads(request.data)
        calls.append(payload)
        if provider == "cloudflare":
            assert request.full_url == f"https://api.cloudflare.com/client/v4/accounts/{'a' * 32}/ai/run/@cf/deepgram/aura-2-en"
            assert request.get_header("Authorization") == "Bearer test-secret"
            assert payload["encoding"] == "linear16"
            assert payload["container"] == "none"
            assert payload["sample_rate"] == 24000
            response = io.BytesIO(data[44:])
            response.headers = {"Content-Type": "audio/pcm"}
            return response
        assert request.full_url == "https://generativelanguage.googleapis.com/v1beta/interactions"
        assert request.get_header("X-goog-api-key") == "test-secret"
        assert payload["model"] == "gemini-3.8-flash-tts"
        assert payload["store"] is False
        assert payload["response_format"] == {"type": "audio"}
        return io.BytesIO(json.dumps({"steps": [{"type": "model_output", "content": [
            {"type": "audio", "data": base64.b64encode(data).decode()}
        ]}]}).encode())

    monkeypatch.setenv("GEMINI_API_KEY", "test-secret")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-secret")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)
    monkeypatch.setattr(engine.urllib.request, "urlopen", respond)
    monkeypatch.setattr(server, "JOBS_DIR", tmp_path / "web")
    monkeypatch.setattr(speak, "JOBS_DIR", tmp_path / "native")
    monkeypatch.setattr(speak, "_default_engine", provider)
    def no_local_model():
        pytest.fail("Cloud speech must not launch the local Kokoro model")
    monkeypatch.setattr(speak, "_ensure_proc", no_local_model)
    return provider, calls, data


def test_key_loading_is_independent_of_cwd_and_keeps_environment_priority(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    env = tmp_path / ".env"
    monkeypatch.setattr(engine, "ENV_FILE", env)
    monkeypatch.chdir(tmp_path.parent)
    assert engine.gemini_api_key() == ""
    env.write_text('# comment\nexport GEMINI_API_KEY="file-secret" # comment\n')
    assert engine.gemini_api_key() == "file-secret"
    monkeypatch.setenv("GOOGLE_API_KEY", "environment-secret")
    assert engine.gemini_api_key() == "environment-secret"


def test_missing_key_is_reported_before_a_native_job_starts(monkeypatch, tmp_path):
    monkeypatch.setattr(speak, "_default_engine", "gemini")
    monkeypatch.setattr(engine, "gemini_api_key", lambda: "")
    client = TestClient(speak.app, base_url="http://127.0.0.1")
    assert client.get("/health").json()["ready"] is False
    response = client.post("/speak", json={"text": "Read me."})
    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.json()["detail"]
    with pytest.raises(engine.SynthError, match="GEMINI_API_KEY"):
        engine.synth_chunk("Read me.", tmp_path / "missing.wav", "gemini")


def test_shared_synthesis_uses_verbatim_text_and_selected_voice(cloud, tmp_path):
    provider, calls, data = cloud
    voice = "Sulafat" if provider == "gemini" else "orion"
    path = engine.synth_chunk("Read this exactly.", tmp_path / "test.wav", provider, voice=voice)
    assert path.read_bytes() == data
    if provider == "gemini":
        assert calls[0]["input"][0]["content"] == [{"type": "text", "text": "Read this exactly."}]
        assert calls[0]["generation_config"]["speech_config"] == [{"voice": voice}]
    else:
        assert calls[0]["text"] == "Read this exactly."
        assert calls[0]["speaker"] == voice


@pytest.mark.parametrize("client_name", ["native", "web"])
def test_both_clients_normalize_and_serve_cloud_wav(cloud, client_name, monkeypatch):
    provider, calls, data = cloud
    client = TestClient(
        speak.app if client_name == "native" else server.app,
        base_url="http://127.0.0.1",
    )
    if client_name == "native":
        monkeypatch.setattr(speak, "_default_engine", "kokoro")
        # A saved local voice from an older app must not break the new default.
        result = client.post("/speak", json={"text": "# Hello **world**", "engine": provider, "voice": "bm_lewis", "calm": True})
        assert result.status_code == 200
        audio = client.get(f'/segment/{result.json()["id"]}/0.wav')
        if provider == "gemini":
            assert calls[0]["generation_config"]["speech_config"] == [{"voice": "Charon"}]
        else:
            assert calls[0]["speaker"] == "jupiter"
    else:
        body = {"markdown": "# Hello **world**", "calm": True, "engine": provider}
        if provider == "gemini":
            body.update(engine="gemini", voice="Kore")
        result = client.post("/api/speak", json=body)
        assert result.status_code == 200
        job_id = result.json()["id"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            job = client.get(f"/api/job/{job_id}").json()
            if job["state"] != "running":
                break
            time.sleep(0.01)
        assert job["state"] == "ready", job
        audio = client.get(f"/api/audio/{job_id}.wav")
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/wav"
    assert audio.headers["cache-control"] == "no-store"
    directory = (speak.JOBS_DIR if client_name == "native" else server.JOBS_DIR) / result.json()["id"]
    assert directory.stat().st_mode & 0o777 == 0o700
    assert audio.content == data
    text = calls[0]["input"][0]["content"][0]["text"] if provider == "gemini" else calls[0]["text"]
    assert text == "Hello world."


@pytest.mark.parametrize("failure, message", [
    (urllib.error.HTTPError("https://example.invalid", 429, "test-secret", {}, None), "quota exhausted"),
    (TimeoutError(), "timed out"),
    ({"steps": []}, "no audio"),
    ({"steps": [{"type": "model_output", "content": [{"type": "audio", "data": "invalid!"}]}]}, "invalid WAV"),
])
def test_failures_are_actionable_and_do_not_publish_audio(cloud, monkeypatch, tmp_path, failure, message):
    provider = cloud[0]
    if provider == "cloudflare":
        message = "quota/rate limit" if message == "quota exhausted" else message
        message = "error instead of audio" if message in {"no audio", "invalid WAV"} else message
    def fail(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        response = io.BytesIO(json.dumps(failure).encode())
        response.headers = {"Content-Type": "application/json"}
        return response

    monkeypatch.setattr(engine.urllib.request, "urlopen", fail)
    path = tmp_path / "failed.wav"
    with pytest.raises(engine.SynthError, match=message) as exc:
        engine.synth_chunk("Test.", path, provider)
    assert "test-secret" not in str(exc.value)
    assert not path.exists()


def test_native_stop_prevents_later_cloud_requests(cloud, monkeypatch):
    job = speak.SpeakJob("cancel-test", total=2)
    monkeypatch.setitem(speak.JOBS, job.id, job)
    render = speak._render_piece

    def render_then_cancel(*args):
        render(*args)
        assert TestClient(speak.app, base_url="http://127.0.0.1").post(f"/cancel/{job.id}").status_code == 200

    monkeypatch.setattr(speak, "_render_piece", render_then_cancel)
    speak._render(job, ["First.", "Second."], speak.SpeakIn(text="First. Second."))
    assert len(cloud[1]) == 1
    assert TestClient(speak.app, base_url="http://127.0.0.1").get(f"/segment/{job.id}/1.wav").status_code == 409


def test_cloudflare_credentials_and_account_validation(monkeypatch, tmp_path):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    env = tmp_path / ".env"
    monkeypatch.setattr(engine, "ENV_FILE", env)
    monkeypatch.setattr(speak, "_default_engine", "cloudflare")
    client = TestClient(speak.app, base_url="http://127.0.0.1")
    assert client.get("/health").json()["ready"] is False
    assert client.post("/speak", json={"text": "Test."}).status_code == 503
    env.write_text(f'CLOUDFLARE_API_TOKEN="secret-token"\nCLOUDFLARE_ACCOUNT_ID={"a" * 32}\n')
    assert engine.cloudflare_credentials() == ("secret-token", "a" * 32)
    assert client.get("/health").json()["ready"] is True
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "../wrong")
    with pytest.raises(engine.SynthError, match="32-character account ID"):
        engine.cloudflare_credentials()


@pytest.mark.parametrize("data", [b"", b"\x01"])
def test_cloudflare_rejects_empty_or_incomplete_pcm(monkeypatch, tmp_path, data):
    monkeypatch.setattr(engine, "cloudflare_credentials", lambda: ("test-secret", "a" * 32))
    response = io.BytesIO(data)
    response.headers = {"Content-Type": "audio/pcm"}
    monkeypatch.setattr(engine.urllib.request, "urlopen", lambda *a, **kw: response)
    with pytest.raises(engine.SynthError, match="incomplete PCM"):
        engine.synth_chunk("Test.", tmp_path / "test.wav", "cloudflare")


def test_cloud_request_validation_errors_do_not_expose_credentials(cloud, monkeypatch, tmp_path):
    def invalid_header(*args, **kwargs):
        raise ValueError("Invalid header value: test-secret")
    monkeypatch.setattr(engine.urllib.request, "urlopen", invalid_header)
    with pytest.raises(engine.SynthError) as error:
        engine.synth_chunk("Synthetic text.", tmp_path / "invalid.wav", cloud[0])
    assert "test-secret" not in str(error.value)


def test_native_default_cloud_engine_ignores_legacy_local_default_voice(cloud, monkeypatch):
    provider, calls, _ = cloud
    monkeypatch.setattr(speak, "_default_voice", "bm_fable")
    client = TestClient(speak.app, base_url="http://127.0.0.1")
    job = client.post("/speak", json={"text": "Synthetic speech."})
    assert job.status_code == 200
    assert client.get(f'/segment/{job.json()["id"]}/0.wav').status_code == 200
    if provider == "cloudflare":
        assert calls[0]["speaker"] == "jupiter"
    else:
        assert calls[0]["generation_config"]["speech_config"] == [{"voice": "Charon"}]


def test_web_audio_requires_a_known_ready_job(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "JOBS_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    (tmp_path / "audio.wav").write_bytes(b"synthetic private bytes")
    client = TestClient(server.app, base_url="http://127.0.0.1")
    assert client.get("/api/audio/...wav").status_code == 404
