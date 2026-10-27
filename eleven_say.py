#!/usr/bin/env python3
"""eleven-say: text -> ElevenLabs -> a Telegram-ready voice note (.ogg / opus).

One command, one file out. Works for any agent that can run a shell command
(Codex, OpenCode, Claude Code, ...); the model behind the agent does not matter.

What it does on top of a raw API call:
  * trims the short junk tail some ElevenLabs models append after the last word
    (but keeps a short final phrase that is real speech);
  * pads a little silence at both ends so Telegram's opus playback does not
    swallow the first/last syllable;
  * re-generates when a Chinese clip is suspiciously long for its text
    (the model sometimes reads a sentence twice);
  * converts to ogg/opus, which Telegram plays as a voice message.

Configuration comes only from environment variables (see .env.example).
Nothing secret lives in this file or in the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL = "eleven_v4"
# Requests are minimal by default: text + model + voice. Voice settings are sent only
# when the user sets them; which ones a model honours is for them to test (see README).
EXPLICIT_VARS = {
    "ELEVENLABS_STABILITY": "stability",
    "ELEVENLABS_SIMILARITY": "similarity_boost",
    "ELEVENLABS_STYLE": "style",
}
SPEED_MIN, SPEED_MAX = 0.7, 1.2

# Silence detection / trimming
SILENCE_DB, SILENCE_MIN = "-50dB", 0.4
TAIL_JUNK_MAX = 0.8  # content after the last long silence shorter than this = junk
HEAD_PAD, TAIL_PAD = 0.30, 0.45
# A short last phrase after a pause is real speech, not junk: estimate how long
# it should take to say and keep it if the tail is long enough.
TAIL_SEC_PER_CJK, TAIL_SEC_PER_WORD, TAIL_KEEP_RATIO = 0.22, 0.25, 0.75
# Repeat guard: normal Chinese narration runs ~0.28-0.34 s per character.
REPEAT_SEC_PER_CJK, REPEAT_RETRIES, REPEAT_MIN_CJK = 0.42, 2, 15

CJK = r"[一-鿿]"
TAG = r"\[[^\]]*\]"


class SayError(RuntimeError):
    """A problem the caller can act on; message is safe to show."""


# ---------- configuration ----------


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def _float_env(name: str, default: float, lo: float, hi: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        v = float(raw)
    except ValueError as e:
        raise SayError(f"{name} 必须是数字，现在是 {raw!r}") from e
    if not lo <= v <= hi:
        raise SayError(f"{name} 要在 {lo}–{hi} 之间，现在是 {v}")
    return v


def _opt_float_env(name: str, lo: float, hi: float) -> float | None:
    return _float_env(name, 0.0, lo, hi) if _env(name) is not None else None


def check_speed(speed: float) -> float:
    if not SPEED_MIN <= speed <= SPEED_MAX:
        raise SayError(f"语速要在 {SPEED_MIN}–{SPEED_MAX} 之间，现在是 {speed}")
    return speed


def api_key() -> str:
    key = _env("ELEVENLABS_API_KEY")
    if key:
        return key
    path = _env("ELEVENLABS_API_KEY_FILE")
    if path:
        try:
            with open(os.path.expanduser(path)) as f:
                key = f.read().strip()
        except OSError as e:
            raise SayError(f"读不到 ELEVENLABS_API_KEY_FILE 指向的文件：{e.strerror}") from e
        if key:
            return key
    raise SayError(
        "没有找到 ElevenLabs 钥匙：设置 ELEVENLABS_API_KEY，或把钥匙存进文件、用 ELEVENLABS_API_KEY_FILE 指过去"
    )


def settings() -> dict:
    voice = _env("ELEVENLABS_VOICE_ID")
    if not voice:
        raise SayError("没有设置 ELEVENLABS_VOICE_ID（在 ElevenLabs 的 Voices 页面复制嗓子的 ID）")
    model = _env("ELEVENLABS_MODEL", DEFAULT_MODEL)
    trim = (_env("ELEVENLABS_TRIM_TAIL", "auto") or "auto").lower()
    if trim not in ("auto", "on", "off"):
        raise SayError("ELEVENLABS_TRIM_TAIL 只能是 auto / on / off")
    return {
        "voice_id": voice,
        "model": model,
        "stability": _float_env("ELEVENLABS_STABILITY", 0.30, 0.0, 1.0),
        "similarity_boost": _float_env("ELEVENLABS_SIMILARITY", 0.75, 0.0, 1.0),
        "style": _float_env("ELEVENLABS_STYLE", 0.0, 0.0, 1.0),
        # per-language speed: None = not set = not sent
        "speed_zh": _opt_float_env("ELEVENLABS_SPEED_ZH", SPEED_MIN, SPEED_MAX),
        "speed_en": _opt_float_env("ELEVENLABS_SPEED_EN", SPEED_MIN, SPEED_MAX),
        # junk tails were seen on v3; other models: only when asked
        "trim_tail": trim == "on" or (trim == "auto" and model == "eleven_v3"),
        # only settings the user sets explicitly are sent
        "explicit": {field for var, field in EXPLICIT_VARS.items() if _env(var)},
    }


def ffmpeg_bins() -> tuple[str, str]:
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ff or not fp:
        raise SayError("找不到 ffmpeg / ffprobe。Debian/Ubuntu：sudo apt install ffmpeg；macOS：brew install ffmpeg")
    return ff, fp


# ---------- API ----------


def redact(detail: object, key: str | None) -> str:
    s = str(detail or "")
    if key:
        s = s.replace(key, "[redacted]")
    return re.sub(r"sk_[A-Za-z0-9_-]+", "[redacted]", s)[:600]


def voice_settings(cfg: dict, speed: float | None) -> dict:
    """Only what the user set explicitly; speed only when one applies to this text."""
    vs = {k: cfg[k] for k in ("stability", "similarity_boost", "style") if k in cfg.get("explicit", set())}
    if speed is not None:
        vs["speed"] = check_speed(float(speed))
    return vs


def build_payload(text: str, cfg: dict, speed: float | None) -> dict:
    payload: dict = {"text": text, "model_id": cfg["model"]}
    vs = voice_settings(cfg, speed)
    if vs:
        payload["voice_settings"] = vs
    return payload


def request_mp3(
    text: str,
    cfg: dict,
    *,
    speed: float | None,
    seed: int | None = None,
    previous_text: str | None = None,
    next_text: str | None = None,
    timeout: int = 120,
) -> bytes:
    key = api_key()
    payload = build_payload(text, cfg, speed)
    if seed is not None:
        payload["seed"] = int(seed)
    if previous_text:
        payload["previous_text"] = previous_text
    if next_text:
        payload["next_text"] = next_text
    voice = urllib.parse.quote(cfg["voice_id"], safe="")
    req = urllib.request.Request(
        url=f"{API_BASE}/text-to-speech/{voice}?output_format=mp3_44100_128",
        data=json.dumps(payload).encode("utf-8"),
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = redact(e.read().decode("utf-8", errors="replace"), key)
        hint = "（这个模型可能不认你设的某个 ELEVENLABS_* 参数，去掉再试）" if e.code == 422 else ""
        raise SayError(f"ElevenLabs 请求失败：HTTP {e.code}{hint} {detail}") from e
    except (urllib.error.URLError, OSError) as e:  # includes timeouts
        raise SayError(f"ElevenLabs 请求失败（网络）：{redact(e, key)}") from e


# ---------- audio ----------


def duration(path: str, ffprobe: str) -> float:
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True,
        text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError as e:
        raise SayError("读不出生成音频的时长（ElevenLabs 返回的可能不是音频）") from e


def silences(path: str, ffmpeg: str) -> tuple[list[float], list[float]]:
    out = subprocess.run(
        [ffmpeg, "-i", path, "-af", f"silencedetect=noise={SILENCE_DB}:d={SILENCE_MIN}", "-f", "null", "-"],
        capture_output=True,
        text=True,
    ).stderr
    starts = [float(m) for m in re.findall(r"silence_start: ([\d.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end: ([\d.]+)", out)]
    return starts, ends


def tail_is_speech(text: str, tail: float) -> bool:
    body = re.sub(TAG, "", text).strip()
    parts = [x for x in re.split(r"[，。！？；、…,.!?;]+", body) if x.strip()]
    if not parts:
        return False
    clause = parts[-1]
    expected = (
        len(re.findall(CJK, clause)) * TAIL_SEC_PER_CJK + len(re.findall(r"[A-Za-z']+", clause)) * TAIL_SEC_PER_WORD
    )
    return expected > 0 and tail >= expected * TAIL_KEEP_RATIO


def trim_point(text: str, dur: float, starts: list[float], ends: list[float]) -> float:
    """Where to cut, or dur when nothing should be cut."""
    if starts and ends and ends[-1] < dur:
        tail = dur - ends[-1]
        if tail <= TAIL_JUNK_MAX and not tail_is_speech(text, tail):
            return starts[-1] + 0.25
    return dur


def looks_repeated(text: str, dur: float) -> bool:
    n = len(re.findall(CJK, re.sub(TAG, "", text)))
    return n >= REPEAT_MIN_CJK and dur / n > REPEAT_SEC_PER_CJK


def pick_speed(cfg: dict, text: str, speed: float | None) -> float | None:
    if speed is not None:
        return check_speed(float(speed))
    return cfg["speed_en"] if not re.search(CJK, text) else cfg["speed_zh"]


def encode(src: str, out_path: str, chain: list[str], ffmpeg: str) -> None:
    """Encode to a temp file next to the target, then atomically replace it (mode 0600)."""
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".eleven-say-", suffix=".ogg", dir=out_dir)
    os.close(fd)
    try:
        r = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-y",
                "-i",
                src,
                "-af",
                ",".join(chain),
                "-c:a",
                "libopus",
                "-b:a",
                "64k",
                "-vbr",
                "on",
                "-application",
                "audio",
                tmp,
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise SayError(f"ffmpeg 转码失败：{r.stderr.strip()[-300:]}")
        os.chmod(tmp, 0o600)
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def synth(
    text: str,
    out_path: str,
    *,
    speed: float | None = None,
    seed: int | None = None,
    previous_text: str | None = None,
    next_text: str | None = None,
) -> tuple[str, float]:
    """Synthesize, trim, pad, encode. Returns (out_path, seconds trimmed).

    A clip that looks repeated is regenerated, so one call can use up to
    REPEAT_RETRIES + 1 generations of credit.
    """
    cfg = settings()
    ffmpeg, ffprobe = ffmpeg_bins()
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        raise SayError("没有要念的文字")
    use_speed = pick_speed(cfg, cleaned, speed)

    with tempfile.TemporaryDirectory(prefix="eleven-say-") as tmp:
        src = os.path.join(tmp, "src.mp3")
        for attempt in range(REPEAT_RETRIES + 1):
            with open(src, "wb") as f:
                f.write(
                    request_mp3(
                        cleaned, cfg, speed=use_speed, seed=seed, previous_text=previous_text, next_text=next_text
                    )
                )
            dur = duration(src, ffprobe)
            if not looks_repeated(cleaned, dur):
                break
            if attempt < REPEAT_RETRIES:
                print(f"eleven-say: 疑似复读（第 {attempt + 1} 次），重新生成", file=sys.stderr)
            else:
                print("eleven-say: 疑似复读，重试已用完，保留最后一条", file=sys.stderr)

        cut = dur
        if cfg["trim_tail"]:
            starts, ends = silences(src, ffmpeg)
            cut = trim_point(cleaned, dur, starts, ends)

        # Trim inside the filter chain: "-t" would also cut the padding added after it.
        chain = [f"atrim=end={cut:.3f}", "asetpts=N/SR/TB"] if cut < dur else []
        chain += [f"adelay={int(HEAD_PAD * 1000)}:all=1", f"apad=pad_dur={TAIL_PAD}"]
        encode(src, out_path, chain, ffmpeg)
    return out_path, round(dur - cut, 3)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eleven-say", description="文字 → Telegram 能直接发的语音 (.ogg)")
    p.add_argument("text", nargs="?", help="要念的文字；不给就从 --file 或标准输入读")
    p.add_argument("-o", "--out", default="voice.ogg", help="输出文件路径（默认 voice.ogg）")
    p.add_argument("-f", "--file", help="从文本文件读")
    p.add_argument("--model", help="模型 ID（默认读 ELEVENLABS_MODEL，再默认 eleven_v4）")
    p.add_argument("--speed", type=float, help="语速 0.7–1.2，设了才发送；模型认不认自己试")
    p.add_argument("--seed", type=int, help="固定随机种子，满意的一条可以同 seed 重出")
    p.add_argument("--previous-text", help="长故事分段时传上一段，语气接得上")
    p.add_argument("--next-text", help="长故事分段时传下一段")
    a = p.parse_args(argv)

    if a.model:
        os.environ["ELEVENLABS_MODEL"] = a.model
    try:
        if a.text is not None:
            text = a.text
        elif a.file:
            try:
                with open(a.file, encoding="utf-8") as f:
                    text = f.read()
            except OSError as e:
                raise SayError(f"读不到文本文件：{e.strerror}") from e
        else:
            text = sys.stdin.read()
        path, trimmed = synth(
            text, a.out, speed=a.speed, seed=a.seed, previous_text=a.previous_text, next_text=a.next_text
        )
    except SayError as e:
        print(f"eleven-say: {e}", file=sys.stderr)
        return 1
    print(path)
    if trimmed:
        print(f"eleven-say: 剪掉了结尾杂音 {trimmed}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
