"""Offline tests: the ElevenLabs API is mocked, no key and no credits needed.

Needs ffmpeg on PATH for the audio tests (they are skipped otherwise).
"""

import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import eleven_say as es  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
PREFIX = "sk" + "_"  # built at runtime so secret scanners do not flag the test file
FAKE_KEY = PREFIX + "test" + "0" * 16


@pytest.fixture
def env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ELEVENLABS_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", FAKE_KEY)
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "voice123")
    return monkeypatch


def make_mp3(path, spec):
    """spec: list of ("tone"|"silence", seconds)."""
    inputs, labels = [], []
    for i, (kind, sec) in enumerate(spec):
        src = f"sine=frequency=440:duration={sec}" if kind == "tone" else f"anullsrc=r=44100:cl=mono:d={sec}"
        inputs += ["-f", "lavfi", "-i", src]
        labels.append(f"[{i}:a]")
    graph = "".join(labels) + f"concat=n={len(spec)}:v=0:a=1[a]"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[a]",
            "-ar",
            "44100",
            "-ac",
            "1",
            path,
        ],
        check=True,
    )


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------- configuration ----------


def test_missing_key(env):
    env.delenv("ELEVENLABS_API_KEY")
    with pytest.raises(es.SayError, match="钥匙"):
        es.api_key()


def test_key_from_file(env, tmp_path):
    env.delenv("ELEVENLABS_API_KEY")
    p = tmp_path / "k"
    p.write_text(FAKE_KEY + "\n")
    env.setenv("ELEVENLABS_API_KEY_FILE", str(p))
    assert es.api_key() == FAKE_KEY


def test_missing_voice(env):
    env.delenv("ELEVENLABS_VOICE_ID")
    with pytest.raises(es.SayError, match="VOICE_ID"):
        es.settings()


def test_bad_number(env):
    env.setenv("ELEVENLABS_STABILITY", "2")
    with pytest.raises(es.SayError, match="0.0–1.0"):
        es.settings()


def test_default_model_is_v4_without_trim(env):
    cfg = es.settings()
    assert cfg["model"] == "eleven_v4" and cfg["trim_tail"] is False


def test_v3_trims_by_default(env):
    env.setenv("ELEVENLABS_MODEL", "eleven_v3")
    assert es.settings()["trim_tail"] is True


def test_v4_sends_bare_request(env):
    p = es.build_payload("[quiet] 你好", es.settings(), None)
    assert p == {"text": "[quiet] 你好", "model_id": "eleven_v4"}


def test_v3_default_is_bare_too(env):
    env.setenv("ELEVENLABS_MODEL", "eleven_v3")
    assert "voice_settings" not in es.build_payload("x", es.settings(), None)


def test_speed_out_of_range_is_an_error(env):
    with pytest.raises(es.SayError, match="语速"):
        es.pick_speed(es.settings(), "你好", 5)
    env.setenv("ELEVENLABS_SPEED_ZH", "1.5")
    with pytest.raises(es.SayError):
        es.settings()


def test_speed_languages_do_not_leak(env):
    env.setenv("ELEVENLABS_SPEED_EN", "0.9")
    cfg = es.settings()
    assert es.pick_speed(cfg, "你好", None) is None
    assert es.pick_speed(cfg, "hello", None) == 0.9
    assert "voice_settings" not in es.build_payload("你好", cfg, es.pick_speed(cfg, "你好", None))


def test_cli_speed_zero_is_not_ignored(env):
    with pytest.raises(es.SayError):
        es.pick_speed(es.settings(), "你好", 0.0)


def test_explicit_setting_is_always_sent(env):
    env.setenv("ELEVENLABS_STYLE", "0.4")
    env.setenv("ELEVENLABS_SPEED_ZH", "0.9")
    cfg = es.settings()
    vs = es.build_payload("你好", cfg, es.pick_speed(cfg, "你好", None))["voice_settings"]
    assert vs == {"style": 0.4, "speed": 0.9}


def test_422_gives_hint(env, monkeypatch):
    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 422, "x", {}, io.BytesIO(b"bad field"))

    monkeypatch.setattr(es.urllib.request, "urlopen", boom)
    with pytest.raises(es.SayError, match="去掉再试"):
        es.request_mp3("x", es.settings(), speed=1)


def test_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr(es.shutil, "which", lambda _: None)
    with pytest.raises(es.SayError, match="apt install ffmpeg"):
        es.ffmpeg_bins()


# ---------- API ----------


def test_redact_hides_key():
    other = PREFIX + "other123"
    s = es.redact(f"bad key {FAKE_KEY} and {other}", FAKE_KEY)
    assert FAKE_KEY not in s and other not in s


def test_http_error_is_redacted(env, monkeypatch):
    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "x", {}, io.BytesIO(f"invalid {FAKE_KEY}".encode()))

    monkeypatch.setattr(es.urllib.request, "urlopen", boom)
    with pytest.raises(es.SayError) as e:
        es.request_mp3("你好", es.settings(), speed=1)
    assert "401" in str(e.value) and FAKE_KEY not in str(e.value)


def test_request_payload(env, monkeypatch):
    seen = {}

    def fake(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        seen["key"] = req.get_header("Xi-api-key")
        return FakeResp(b"mp3")

    monkeypatch.setattr(es.urllib.request, "urlopen", fake)
    assert es.request_mp3("hi", es.settings(), speed=1, seed=7, previous_text="a") == b"mp3"
    assert "/text-to-speech/voice123" in seen["url"]
    assert seen["body"]["model_id"] == "eleven_v4" and seen["body"]["seed"] == 7
    assert seen["body"]["previous_text"] == "a" and seen["key"] == FAKE_KEY


def test_timeout_is_a_say_error(env, monkeypatch):
    def boom(req, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(es.urllib.request, "urlopen", boom)
    with pytest.raises(es.SayError, match="网络"):
        es.request_mp3("x", es.settings(), speed=None)


# ---------- pure logic ----------


def test_tail_is_speech_keeps_short_last_phrase():
    assert es.tail_is_speech("……我在这儿。", 0.8)
    assert not es.tail_is_speech("今天很好。", 0.1)


def test_trim_point():
    assert es.trim_point("今天我们一起出去散步吧。", 5.0, [3.0], [4.5]) == pytest.approx(3.25)
    assert es.trim_point("你好。", 5.0, [3.0], [4.5]) == 5.0  # short last phrase = real speech
    assert es.trim_point("你好。", 5.0, [], []) == 5.0


def test_repeat_guard():
    text = "一" * 20
    assert es.looks_repeated(text, 20 * 0.5)
    assert not es.looks_repeated(text, 20 * 0.3)
    assert not es.looks_repeated("短句", 10)


# ---------- end to end with fake audio ----------


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_synth_end_to_end(env, monkeypatch, tmp_path):
    env.setenv("ELEVENLABS_MODEL", "eleven_v3")
    src = tmp_path / "fake.mp3"
    make_mp3(str(src), [("tone", 2.0), ("silence", 1.0), ("tone", 0.3)])
    monkeypatch.setattr(es.urllib.request, "urlopen", lambda req, timeout: FakeResp(src.read_bytes()))
    out = tmp_path / "o" / "v.ogg"
    path, trimmed = es.synth("今天很好。", str(out))
    assert os.path.exists(path) and trimmed > 0
    codec = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
    )
    assert codec.stdout.strip() == "opus"


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_output_is_private_and_atomic(env, monkeypatch, tmp_path):
    src = tmp_path / "fake.mp3"
    make_mp3(str(src), [("tone", 1.0)])
    monkeypatch.setattr(es.urllib.request, "urlopen", lambda req, timeout: FakeResp(src.read_bytes()))
    out = tmp_path / "v.ogg"
    es.synth("你好", str(out))
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    # a failing encode must leave the old file untouched and no temp files behind
    out.write_bytes(b"old")
    monkeypatch.setattr(es.urllib.request, "urlopen", lambda req, timeout: FakeResp(b"not audio"))
    with pytest.raises(es.SayError):
        es.synth("你好", str(out))
    assert out.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".eleven-say-")] == []


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_bad_audio_is_a_say_error(env, monkeypatch, tmp_path):
    monkeypatch.setattr(es.urllib.request, "urlopen", lambda req, timeout: FakeResp(b"<html>"))
    with pytest.raises(es.SayError, match="时长"):
        es.synth("你好", str(tmp_path / "v.ogg"))


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_repeat_exhausted_says_so(env, monkeypatch, tmp_path, capsys):
    long_mp3 = tmp_path / "long.mp3"
    make_mp3(str(long_mp3), [("tone", 12.0)])
    calls = []

    def fake(req, timeout):
        calls.append(1)
        return FakeResp(long_mp3.read_bytes())

    monkeypatch.setattr(es.urllib.request, "urlopen", fake)
    es.synth("一" * 20, str(tmp_path / "v.ogg"))
    err = capsys.readouterr().err
    assert len(calls) == es.REPEAT_RETRIES + 1 and "重试已用完" in err


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_repeat_regenerates(env, monkeypatch, tmp_path):
    long_mp3, ok_mp3 = tmp_path / "long.mp3", tmp_path / "ok.mp3"
    make_mp3(str(long_mp3), [("tone", 12.0)])
    make_mp3(str(ok_mp3), [("tone", 6.0)])
    calls = []

    def fake(req, timeout):
        calls.append(1)
        return FakeResp((long_mp3 if len(calls) == 1 else ok_mp3).read_bytes())

    monkeypatch.setattr(es.urllib.request, "urlopen", fake)
    es.synth("一" * 20, str(tmp_path / "v.ogg"))
    assert len(calls) == 2


def test_cli_reports_error(env, capsys):
    env.delenv("ELEVENLABS_VOICE_ID")
    assert es.main(["你好"]) == 1
    assert "VOICE_ID" in capsys.readouterr().err
