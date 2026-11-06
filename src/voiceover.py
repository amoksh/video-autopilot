"""Stage 3 — synthesize the voiceover. edge-tts (free) or ElevenLabs (premium)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from .config import env
from .utils import ffprobe_duration, log, run_ffmpeg


def synthesize(cfg: dict, text: str, out_dir: Path) -> Path:
    raw = out_dir / "voice_raw.mp3"
    final = out_dir / "voice.mp3"
    provider = cfg["tts"]["provider"]

    if provider == "edge":
        _edge_tts(cfg, text, raw)
    elif provider == "elevenlabs":
        _elevenlabs(cfg, text, raw)
    else:
        raise ValueError(f"Unknown tts.provider: {provider}")

    speed = cfg["tts"].get("speed", 1.0)
    if cfg["tts"].get("studio_safe_mastering"):
        chain = (
            f"atempo={speed},highpass=f=70,"
            "loudnorm=I=-18:TP=-2:LRA=11,"
            "alimiter=limit=0.8:attack=5:release=50:level=false"
        )
    else:
        # Existing pipeline mastering remains unchanged.
        chain = (
            f"atempo={speed},"
            "highpass=f=85,"
            "acompressor=threshold=-20dB:ratio=3.5:attack=5:release=140:makeup=3,"
            "equalizer=f=180:t=q:w=1.0:g=-2,"
            "equalizer=f=3200:t=q:w=1.6:g=3,"
            "treble=g=2:f=9000,"
            "deesser=i=0.35,"
            "loudnorm=I=-16:TP=-1.5:LRA=11,"
            "alimiter=limit=0.95"
        )
    run_ffmpeg([
        "-i", str(raw),
        "-filter:a", chain,
        "-ar", "48000", str(final),
    ])
    dur = ffprobe_duration(final)
    log(f"voiceover: voice.mp3 ({dur:.1f}s, {provider}, mastered)", "ok")
    return final


def _edge_tts(cfg: dict, text: str, out: Path) -> None:
    import edge_tts

    voice = cfg["tts"].get("edge_voice", "en-US-AriaNeural")
    rate = cfg["tts"].get("edge_rate", "+0%")     # e.g. "+8%" for more energy
    pitch = cfg["tts"].get("edge_pitch", "+0Hz")  # e.g. "+2Hz" for warmth

    async def _run():
        await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(str(out))

    asyncio.run(_run())


def _elevenlabs(cfg: dict, text: str, out: Path) -> None:
    import requests

    key = env("ELEVENLABS_API_KEY")
    if not key:
        raise SystemExit("ELEVENLABS_API_KEY missing in .env")
    voice = cfg["tts"].get("elevenlabs_voice", "Rachel")
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        json={
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": cfg["tts"].get("elevenlabs_stability", 0.4),
                "similarity_boost": 0.75,
            },
        },
        timeout=120,
    )
    r.raise_for_status()
    out.write_bytes(r.content)
