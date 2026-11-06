"""Stage 6 — composite everything into a clean, cinematic 9:16 master with ffmpeg.

clips → normalized 1080x1920 segments with alternating Ken Burns motion → concat
→ + mastered voiceover + ducked music → color grade + vignette → burn animated captions
+ hook title → progress bar → platform -14 LUFS. One watermark-free master for all platforms.
"""
from __future__ import annotations

import math
import random
import subprocess
from pathlib import Path
from typing import Optional

from .config import ROOT
from .utils import ffprobe_duration, log, run_ffmpeg

MUSIC_DIR = ROOT / "assets" / "music"
W, H = 1080, 1920


def _video_frame_count(path: Path) -> int:
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    try:
        return int(probe.stdout.strip())
    except ValueError as error:
        raise RuntimeError(f"Could not verify video frame count for {path.name}") from error


def _export_encoding(export_preset: str) -> tuple[str, str, str, int]:
    if export_preset == "legacy":
        return "ultrafast", "20", "192k", 0
    if export_preset == "balanced":
        return "veryfast", "23", "160k", 2
    if export_preset == "high_quality":
        return "fast", "18", "192k", 2
    raise ValueError(f"Unknown export quality preset: {export_preset}")


def _normalize_segment(src: Path, dest: Path, seconds: float, fps: int, idx: int,
                      image_motion: bool = True,
                      encoding: Optional[tuple[str, str, str, int]] = None) -> None:
    """Scale+crop any image/video to a fixed-length 9:16 segment.

    Still images optionally get a restrained zoom and pan. Every segment is bounded by an
    exact output frame count to keep its requested timeline duration.
    """
    is_image = src.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
    frames = max(1, math.ceil(seconds * fps - 1e-9))
    if is_image:
        if image_motion:
            denominator = max(frames - 1, 1)
            zoom = "1+0.045*on/{d}" if idx % 2 == 0 else "1.045-0.045*on/{d}"
            pan = "24*on/{d}" if idx % 2 == 0 else "-24*on/{d}"
            vf = (
                f"scale={W*2}:{H*2}:force_original_aspect_ratio=increase,"
                f"crop={W*2}:{H*2},"
                f"zoompan=z='{zoom.format(d=denominator)}':"
                f"x='iw/2-(iw/zoom/2)+{pan.format(d=denominator)}':"
                f"y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={fps},setsar=1"
            )
        else:
            vf = (
                f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},setsar=1,fps={fps}"
            )
        args = [
            "-loop", "1", "-framerate", str(fps), "-i", str(src),
            "-vf", vf, "-frames:v", str(frames), "-pix_fmt", "yuv420p",
            "-an", str(dest),
        ]
    else:
        # real footage already moves; just fit it cleanly to 9:16
        vf = (
            f"scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},setsar=1,fps={fps}"
        )
        args = [
            "-stream_loop", "-1", "-i", str(src), "-vf", vf,
            "-frames:v", str(frames), "-pix_fmt", "yuv420p",
            "-an", str(dest),
        ]
    if encoding is not None:
        preset, crf, _, threads = encoding
        args[-1:-1] = [
            "-c:v", "libx264", "-preset", preset, "-crf", crf,
            *(["-threads", str(threads)] if threads else []),
        ]
    run_ffmpeg(args)


def assemble(cfg: dict, clips: list, voice: Path, captions: Optional[Path],
             out_dir: Path) -> Path:
    vcfg = cfg["video"]
    fps = vcfg["fps"]
    cut = vcfg["cut_every_seconds"]
    voice_dur = ffprobe_duration(voice)
    total = voice_dur + 0.6  # small tail so audio doesn't clip
    seg_dir = out_dir / "segments"
    seg_dir.mkdir(exist_ok=True)

    # Build enough normalized segments to cover the voiceover.
    n_segments = max(1, int(total // cut) + 1)
    segments = []
    for i in range(n_segments):
        src = clips[i % len(clips)]
        dest = seg_dir / f"seg{i:02d}.mp4"
        _normalize_segment(src, dest, cut, fps, i)
        segments.append(dest)

    # Concat the silent video track.
    concat_list = seg_dir / "list.txt"
    concat_list.write_text("".join(f"file '{s.name}'\n" for s in segments), encoding="utf-8")
    silent = out_dir / "silent.mp4"
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-t", f"{total}", "-c", "copy", str(silent)])

    # Audio mix: mastered voiceover + ducked background music (if any track present).
    music = _pick_music()
    mixed = out_dir / "mixed_audio.m4a"
    music_vol = vcfg["music_volume_db"]
    if music:
        # sidechain-duck the music under the voice so narration always stays clear.
        run_ffmpeg([
            "-i", str(voice), "-stream_loop", "-1", "-i", str(music),
            "-filter_complex",
            f"[1:a]volume={music_vol}dB[mv];"
            f"[mv][0:a]sidechaincompress=threshold=0.03:ratio=8:attack=5:release=300[mduck];"
            f"[0:a][mduck]amix=inputs=2:duration=first:dropout_transition=0,"
            f"loudnorm=I=-14:TP=-1.0:LRA=11[a]",
            "-map", "[a]", "-t", f"{total}", "-c:a", "aac", "-b:a", "256k", str(mixed),
        ])
        audio = mixed
    else:
        # no music: still bring the final voice to platform loudness.
        run_ffmpeg([
            "-i", str(voice), "-filter:a", "loudnorm=I=-14:TP=-1.0:LRA=11",
            "-t", f"{total}", "-c:a", "aac", "-b:a", "256k", str(mixed),
        ])
        audio = mixed

    # ── Final video filter chain: grade → vignette → captions → progress bar ──
    vf_filters = []
    if vcfg.get("color_grade", True):
        # cinematic punch: contrast, saturation, micro-lift, gentle sharpen
        vf_filters.append("eq=contrast=1.07:saturation=1.14:brightness=0.012:gamma=0.98")
        vf_filters.append("unsharp=5:5:0.5:5:5:0.0")
        vf_filters.append("vignette=PI/4.5")
    if captions and captions.exists():
        font_dir = (ROOT / "assets" / "fonts").as_posix()
        ass = captions.as_posix().replace(":", r"\:")
        vf_filters.append(f"subtitles='{ass}':fontsdir='{font_dir}'")
    if vcfg.get("add_progress_bar"):
        bar = vcfg.get("progress_bar_color", "0x06D6A0")  # accent, not plain white
        vf_filters.append(
            f"drawbox=x=0:y=ih-10:w='iw*t/{total:.2f}':h=10:color={bar}@0.9:t=fill"
        )
    vf = ",".join(vf_filters) if vf_filters else "null"

    final = out_dir / "final.mp4"
    run_ffmpeg([
        "-i", str(silent), "-i", str(audio),
        "-vf", vf,
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", vcfg.get("encode_preset", "slow"),
        "-crf", str(vcfg.get("crf", 19)),
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", "256k", "-ar", "48000",
        "-t", f"{total}", "-movflags", "+faststart", str(final),
    ])
    log(f"assemble: final.mp4 ({W}x{H}, {ffprobe_duration(final):.1f}s, graded)", "ok")
    return final


def assemble_media_only(clips: list[Path], out_dir: Path, fps: int,
                        seconds_per_scene: float,
                        scene_durations: Optional[list[float]] = None,
                        audio_path: Optional[Path] = None,
                        captions_path: Optional[Path] = None,
                        image_motion: bool = True,
                        transition: str = "hard_cut",
                        export_preset: str = "legacy") -> tuple[Path, float]:
    """Assemble ordered project media into a silent video without pipeline services."""
    if not clips:
        raise ValueError("At least one media clip is required")
    if (isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 120
            or isinstance(seconds_per_scene, bool)
            or not isinstance(seconds_per_scene, (int, float))
            or not math.isfinite(seconds_per_scene) or seconds_per_scene <= 0):
        raise ValueError("Invalid media-only render settings")
    requested_durations = (
        scene_durations if scene_durations is not None
        else [seconds_per_scene] * len(clips)
    )
    if (len(requested_durations) != len(clips)
            or any(
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration)
                or not 0 < duration <= 600
                for duration in requested_durations
            )):
        raise ValueError("Scene durations must match the clips and be between 0 and 600 seconds")
    if sum(requested_durations) > 600:
        raise ValueError("Total render duration exceeds the 600-second limit")
    durations = (
        [math.ceil(duration * fps - 1e-9) / fps for duration in requested_durations]
        if scene_durations is not None else requested_durations
    )
    if any(duration > 600 for duration in durations):
        raise ValueError("Scene duration exceeds the 600-second render limit")
    if (not isinstance(image_motion, bool)
            or transition not in ("hard_cut", "crossfade")
            or export_preset not in ("legacy", "balanced", "high_quality")):
        raise ValueError("Invalid image motion or scene transition setting")
    encoding = _export_encoding(export_preset)
    segment_encoding = encoding
    if (export_preset != "legacy"
            and (transition == "crossfade"
                 or captions_path is not None)):
        segment_encoding = (encoding[0], "18", encoding[2], encoding[3])
    if transition == "crossfade" and (audio_path is not None or captions_path is not None):
        raise ValueError(
            "Crossfade is only available without voiceover or captions so their "
            "timing cannot become desynchronized"
        )
    durations = [math.ceil(duration * fps - 1e-9) / fps for duration in durations]
    if sum(durations) > 600:
        raise ValueError("Frame-aligned total render duration exceeds the 600-second limit")

    seg_dir = out_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    segments = []
    for index, (src, duration) in enumerate(zip(clips, durations)):
        segment = seg_dir / f"seg{index:04d}.mp4"
        _normalize_segment(
            src, segment, duration, fps, index, image_motion,
            segment_encoding if export_preset != "legacy" else None,
        )
        expected_frames = round(duration * fps)
        actual_frames = _video_frame_count(segment)
        if actual_frames != expected_frames:
            raise RuntimeError(
                f"Normalized scene {index + 1} has {actual_frames} frames; "
                f"expected {expected_frames}"
            )
        segments.append(segment)

    silent = out_dir / "silent.mp4"
    transition_frames = max(1, round(0.4 * fps))
    overlap_frames = (
        transition_frames * (len(segments) - 1)
        if transition == "crossfade" else 0
    )
    output_frames = round(sum(durations) * fps) - overlap_frames
    total = output_frames / fps
    if transition == "crossfade" and len(segments) > 1:
        if any(round(duration * fps) <= transition_frames for duration in durations):
            raise ValueError("Every scene must be longer than the crossfade overlap")
        args = []
        filters = []
        for index, segment in enumerate(segments):
            args.extend(["-i", str(segment)])
            filters.append(f"[{index}:v]settb=AVTB,setpts=PTS-STARTPTS[v{index}]")
        prior_frames = round(durations[0] * fps)
        current = "[v0]"
        for index in range(1, len(segments)):
            output_label = f"[xf{index}]"
            offset = (prior_frames - transition_frames) / fps
            filters.append(
                f"{current}[v{index}]xfade=transition=fade:"
                f"duration={transition_frames / fps:.9f}:offset={offset:.9f}"
                f"{output_label}"
            )
            prior_frames += round(durations[index] * fps) - transition_frames
            current = output_label
        args.extend([
            "-filter_complex", ";".join(filters), "-map", current,
            "-an", "-c:v", "libx264", "-preset", encoding[0], "-crf", encoding[1],
            *(["-threads", str(encoding[3])] if encoding[3] else []),
            "-pix_fmt", "yuv420p", "-r", str(fps),
            "-frames:v", str(output_frames), "-t", f"{total:.9f}",
            "-movflags", "+faststart", str(silent),
        ])
        run_ffmpeg(args)
    else:
        concat_list = seg_dir / "list.txt"
        concat_list.write_text(
            "".join(f"file '{segment.name}'\n" for segment in segments),
            encoding="utf-8",
        )
        run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-frames:v", str(output_frames), "-c", "copy",
            "-movflags", "+faststart", str(silent),
        ])

    final = out_dir / "final.mp4"
    if audio_path is None and captions_path is None:
        silent.replace(final)
    else:
        copy_video = (
            export_preset != "legacy"
            and audio_path is not None
            and captions_path is None
        )
        vf = "null"
        if captions_path is not None:
            expected_captions = (out_dir / "captions.ass").resolve()
            if captions_path.resolve() != expected_captions or not expected_captions.is_file():
                raise ValueError("Captions must use the generated project-local captions.ass file")
            vf = "subtitles=filename=captions.ass"
        args = ["-i", str(silent)]
        if audio_path is not None:
            args.extend(["-i", str(audio_path)])
        if not copy_video:
            args.extend(["-vf", vf])
        args.extend(["-map", "0:v:0"])
        if audio_path is not None:
            args.extend([
                "-map", "1:a:0", "-c:a", "aac", "-b:a", encoding[2],
                "-ar", "48000",
            ])
        else:
            args.append("-an")
        if copy_video:
            args.extend(["-c:v", "copy"])
        else:
            args.extend([
                "-c:v", "libx264", "-preset", encoding[0], "-crf", encoding[1],
                *(["-threads", str(encoding[3])] if encoding[3] else []),
                "-pix_fmt", "yuv420p", "-r", str(fps),
                "-frames:v", str(output_frames),
            ])
        args.extend([
            "-t", f"{total:.6f}", "-movflags", "+faststart", str(final),
        ])
        run_ffmpeg(args, cwd=out_dir)
        silent.unlink()
    actual_frames = _video_frame_count(final)
    if actual_frames != output_frames:
        raise RuntimeError(
            f"Rendered video has {actual_frames} frames; expected {output_frames}"
        )
    actual_duration = ffprobe_duration(final)
    if abs(actual_duration - total) > (1 / fps) + 0.04:
        raise RuntimeError(
            "Rendered duration does not match the frame-aligned scene timeline "
            f"(expected {total:.3f}s, got {actual_duration:.3f}s)"
        )
    return final, actual_duration


def _pick_music() -> Optional[Path]:
    tracks = [p for p in MUSIC_DIR.glob("*") if p.suffix.lower() in (".mp3", ".m4a", ".wav")]
    return random.choice(tracks) if tracks else None
