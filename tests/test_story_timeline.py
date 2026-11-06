import json
import subprocess
import struct
import tempfile
import threading
import time
import unittest
import uuid
import zlib
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from src.assemble import _normalize_segment, assemble_media_only
from src.captions import build_scene_captions
from src.output_profiles import get_output_profile
from src.utils import ffprobe_duration, run_ffmpeg as run_ffmpeg_command
from webapp import server


def _write_solid_png(path: Path, color: tuple[int, int, int]) -> None:
    width = height = 32
    raw = b"".join(b"\0" + bytes(color) * width for _ in range(height))

    def chunk(name: bytes, data: bytes) -> bytes:
        body = name + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _make_jpg(path: Path, color: str) -> None:
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=64x64",
            "-frames:v", "1", "-q:v", "2", str(path),
        ],
        check=True, capture_output=True,
    )


def _make_mp4(path: Path, color: str) -> None:
    import subprocess

    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=64x64:r=12:d=1",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True,
    )


def _make_sized_jpg(path: Path, color: str, size: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s={size}",
            "-frames:v", "1", "-q:v", "2", str(path),
        ],
        check=True, capture_output=True,
    )


def _make_sized_mp4(path: Path, color: str, size: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=12:d=1",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True,
    )


def _make_split_mp4(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            "color=c=black:s=160x90:r=12:d=1,"
            "drawbox=x=0:y=0:w=80:h=90:color=red:t=fill,"
            "drawbox=x=80:y=0:w=80:h=90:color=blue:t=fill",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True,
    )


class StoryTimelineIntegrationTest(unittest.TestCase):
    def test_export_metrics_reject_incompatible_video_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "export.mp4"
            path.touch()
            valid_video = {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1080,
                "height": 1920,
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "30/1",
                "duration": "2",
                "bit_rate": "2000000",
            }
            for changes, message in (
                ({"width": 720, "height": 1280}, "1080x1920"),
                ({"pix_fmt": "yuv422p"}, "yuv420p"),
                ({"avg_frame_rate": "24/1"}, "30 FPS"),
            ):
                with self.subTest(changes=changes):
                    video = {**valid_video, **changes}
                    probe = subprocess.CompletedProcess(
                        args=["ffprobe"],
                        returncode=0,
                        stdout=json.dumps({
                            "streams": [video],
                            "format": {"duration": "2"},
                        }),
                        stderr="",
                    )
                    with patch("webapp.server.subprocess.run", return_value=probe):
                        with self.assertRaisesRegex(RuntimeError, message):
                            server._studio_export_metrics(
                                path, expected_duration=2,
                                expected_audio=False, fps=30,
                                last_caption_end=None,
                            )
            landscape_video = {**valid_video, "width": 1920, "height": 1080}
            probe = subprocess.CompletedProcess(
                args=["ffprobe"],
                returncode=0,
                stdout=json.dumps({
                    "streams": [landscape_video],
                    "format": {"duration": "2"},
                }),
                stderr="",
            )
            with patch("webapp.server.subprocess.run", return_value=probe):
                metrics = server._studio_export_metrics(
                    path, expected_duration=2, expected_audio=False,
                    fps=30, last_caption_end=None, aspect_ratio="16:9",
                )
            self.assertEqual((metrics["width"], metrics["height"]), (1920, 1080))

    def test_two_second_still_segments_have_exact_48_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            image = directory / "still.png"
            _write_solid_png(image, (70, 90, 110))
            for motion in (False, True):
                segment = directory / f"still-{motion}.mp4"
                _normalize_segment(image, segment, 2, 24, 0, motion)
                probe = subprocess.run(
                    [
                        "ffprobe", "-v", "error", "-count_frames",
                        "-select_streams", "v:0", "-show_entries",
                        "stream=nb_read_frames,width,height,duration",
                        "-of", "json", str(segment),
                    ],
                    check=True, capture_output=True, text=True,
                )
                stream = json.loads(probe.stdout)["streams"][0]
                self.assertEqual(stream["nb_read_frames"], "48")
                self.assertEqual((stream["width"], stream["height"]), (1080, 1920))
                self.assertAlmostEqual(float(stream["duration"]), 2, delta=1 / 24)

            fractional_segment = directory / "fractional.mp4"
            _normalize_segment(image, fractional_segment, 1.1, 24, 0, True)
            fractional_probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-count_frames",
                    "-select_streams", "v:0", "-show_entries",
                    "stream=nb_read_frames,duration", "-of", "json",
                    str(fractional_segment),
                ],
                check=True, capture_output=True, text=True,
            )
            fractional_stream = json.loads(fractional_probe.stdout)["streams"][0]
            self.assertEqual(fractional_stream["nb_read_frames"], "27")
            self.assertAlmostEqual(float(fractional_stream["duration"]), 27 / 24)

    def test_mixed_media_fills_both_profiles_with_captions_and_aac(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            portrait_image = directory / "portrait.jpg"
            landscape_image = directory / "landscape.jpg"
            portrait_video = directory / "portrait.mp4"
            landscape_video = directory / "landscape.mp4"
            rotated_video = directory / "rotated-landscape.mp4"
            _make_sized_jpg(portrait_image, "red", "90x160")
            _make_sized_jpg(landscape_image, "blue", "160x90")
            _make_sized_mp4(portrait_video, "green", "90x160")
            _make_sized_mp4(landscape_video, "yellow", "160x90")
            rotated_base = directory / "rotated-base.mp4"
            _make_sized_mp4(rotated_base, "cyan", "160x90")
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-display_rotation", "90", "-i", str(rotated_base),
                    "-c", "copy", str(rotated_video),
                ],
                check=True, capture_output=True,
            )
            rotated_probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries",
                    "stream_side_data=rotation", "-of", "json",
                    str(rotated_video),
                ],
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(
                json.loads(rotated_probe.stdout)["streams"][0]["side_data_list"][0]["rotation"],
                90,
            )
            clips = [
                portrait_image, landscape_image, portrait_video,
                landscape_video, rotated_video,
            ]
            original_bytes = [clip.read_bytes() for clip in clips]
            audio = directory / "fixture.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2.5",
                    "-c:a", "pcm_s16le", str(audio),
                ],
                check=True, capture_output=True,
            )
            expected_dominant_channels = [0, 2, 1, 0, 1]

            for aspect_ratio in ("9:16", "16:9"):
                with self.subTest(aspect_ratio=aspect_ratio):
                    profile = get_output_profile(aspect_ratio)
                    out_dir = directory / aspect_ratio.replace(":", "-")
                    out_dir.mkdir()
                    captions = build_scene_captions(
                        [{"text": "Both formats keep captions inside the safe area."}],
                        [{
                            "start": 0.0, "end": 2.5,
                            "caption_end": 2.5,
                        }],
                        out_dir / "captions.ass",
                        profile.width, profile.height,
                    )
                    output, duration = assemble_media_only(
                        clips, out_dir, 12, 0.5,
                        scene_durations=[0.5] * len(clips),
                        audio_path=audio,
                        captions_path=captions,
                        image_motion=False,
                        export_preset="balanced",
                        aspect_ratio=aspect_ratio,
                    )
                    self.assertAlmostEqual(duration, 2.5, delta=1 / 12)
                    probe = subprocess.run(
                        [
                            "ffprobe", "-v", "error", "-count_frames",
                            "-show_entries",
                            "stream=codec_type,codec_name,width,height,pix_fmt,nb_read_frames",
                            "-of", "json", str(output),
                        ],
                        check=True, capture_output=True, text=True,
                    )
                    streams = json.loads(probe.stdout)["streams"]
                    video = next(item for item in streams if item["codec_type"] == "video")
                    audio_stream = next(item for item in streams if item["codec_type"] == "audio")
                    self.assertEqual(video["codec_name"], "h264")
                    self.assertEqual(video["pix_fmt"], "yuv420p")
                    self.assertEqual(
                        (video["width"], video["height"]),
                        (profile.width, profile.height),
                    )
                    self.assertEqual(video["nb_read_frames"], "30")
                    self.assertEqual(audio_stream["codec_name"], "aac")

                    for index, expected_channel in enumerate(expected_dominant_channels):
                        frame = subprocess.run(
                            [
                                "ffmpeg", "-v", "error", "-ss",
                                str(index * 0.5 + 0.2), "-i", str(output),
                                "-frames:v", "1", "-vf", "format=rgb24",
                                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                            ],
                            check=True, capture_output=True,
                        ).stdout
                        self.assertEqual(len(frame), profile.width * profile.height * 3)

                        def pixel(x: int, y: int) -> tuple[int, int, int]:
                            offset = (y * profile.width + x) * 3
                            return tuple(frame[offset:offset + 3])

                        center = pixel(profile.width // 2, profile.height // 2)
                        self.assertEqual(max(range(3), key=center.__getitem__),
                                         expected_channel)
                        for x, y in (
                            (0, 0), (profile.width - 1, 0),
                            (0, profile.height - 1),
                            (profile.width - 1, profile.height - 1),
                        ):
                            self.assertGreater(sum(pixel(x, y)), 40)

                    gray_frame = subprocess.run(
                        [
                            "ffmpeg", "-v", "error", "-ss", "0.2", "-i",
                            str(output), "-frames:v", "1", "-vf", "format=gray",
                            "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
                        ],
                        check=True, capture_output=True,
                    ).stdout
                    bright_positions = [
                        (index % profile.width, index // profile.width)
                        for index, value in enumerate(gray_frame) if value > 200
                    ]
                    self.assertGreater(len(bright_positions), 20)
                    self.assertGreaterEqual(
                        min(x for x, _ in bright_positions),
                        round(profile.width * 0.08),
                    )
                    self.assertLessEqual(
                        max(x for x, _ in bright_positions),
                        round(profile.width * 0.92),
                    )
                    self.assertGreaterEqual(
                        min(y for _, y in bright_positions),
                        round(profile.height * 0.5),
                    )
                    self.assertLessEqual(
                        max(y for _, y in bright_positions),
                        round(profile.height * 0.9),
                    )

                    silent_dir = out_dir / "silent"
                    silent_dir.mkdir()
                    silent_output, silent_duration = assemble_media_only(
                        clips[:2], silent_dir, 12, 0.5,
                        scene_durations=[0.5, 0.5],
                        image_motion=False,
                        export_preset="balanced",
                        aspect_ratio=aspect_ratio,
                    )
                    self.assertAlmostEqual(silent_duration, 1.0, delta=1 / 12)
                    silent_probe = subprocess.run(
                        [
                            "ffprobe", "-v", "error", "-count_frames",
                            "-show_entries",
                            "stream=codec_type,codec_name,width,height,nb_read_frames",
                            "-of", "json", str(silent_output),
                        ],
                        check=True, capture_output=True, text=True,
                    )
                    silent_streams = json.loads(silent_probe.stdout)["streams"]
                    self.assertEqual(len(silent_streams), 1)
                    self.assertEqual(silent_streams[0]["codec_type"], "video")
                    self.assertEqual(silent_streams[0]["codec_name"], "h264")
                    self.assertEqual(
                        (silent_streams[0]["width"], silent_streams[0]["height"]),
                        (profile.width, profile.height),
                    )
                    self.assertEqual(silent_streams[0]["nb_read_frames"], "12")
            self.assertEqual([clip.read_bytes() for clip in clips], original_bytes)

    def test_video_display_rotation_is_applied_before_profile_framing(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "split-landscape.mp4"
            rotated = directory / "split-rotated.mp4"
            output = directory / "normalized.mp4"
            _make_split_mp4(source)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-display_rotation", "90", "-i", str(source),
                    "-c", "copy", str(rotated),
                ],
                check=True, capture_output=True,
            )
            _normalize_segment(
                rotated, output, 1, 12, 0, image_motion=False,
                aspect_ratio="9:16",
            )
            frame = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-ss", "0.25", "-i",
                    str(output), "-frames:v", "1", "-vf", "format=rgb24",
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                ],
                check=True, capture_output=True,
            ).stdout
            profile = get_output_profile("9:16")

            def dominant_at(y_ratio: float) -> int:
                offset = (
                    (round(profile.height * y_ratio) * profile.width
                     + profile.width // 2) * 3
                )
                rgb = tuple(frame[offset:offset + 3])
                return max(range(3), key=rgb.__getitem__)

            self.assertEqual(len(frame), profile.width * profile.height * 3)
            self.assertEqual(
                {dominant_at(0.25), dominant_at(0.75)},
                {0, 2},
            )

    def test_mixed_jpg_mp4_hard_cut_render_preserves_order_duration_and_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            clips = [
                directory / "first.jpg",
                directory / "middle.mp4",
                directory / "last.jpg",
            ]
            _make_jpg(clips[0], "red")
            _make_mp4(clips[1], "blue")
            _make_jpg(clips[2], "green")
            source_bytes = [clip.read_bytes() for clip in clips]
            output, duration = assemble_media_only(
                clips, directory / "hard-cuts", 12, 0.75,
                image_motion=False, transition="hard_cut",
            )
            self.assertAlmostEqual(duration, 2.25, delta=1 / 12)
            info = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries",
                    "stream=codec_type,width,height:format=duration",
                    "-of", "json", str(output),
                ],
                check=True, capture_output=True, text=True,
            )
            probe = json.loads(info.stdout)
            video = next(item for item in probe["streams"]
                         if item["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (1080, 1920))
            self.assertFalse(any(item["codec_type"] == "audio"
                                 for item in probe["streams"]))
            self.assertAlmostEqual(float(probe["format"]["duration"]), 2.25, delta=1 / 12)
            frame_probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-count_frames",
                    "-select_streams", "v:0", "-show_entries",
                    "stream=nb_read_frames", "-of", "json", str(output),
                ],
                check=True, capture_output=True, text=True,
            )
            self.assertEqual(
                json.loads(frame_probe.stdout)["streams"][0]["nb_read_frames"],
                "27",
            )

            def sample_rgb(seconds: float) -> tuple[int, int, int]:
                frame = subprocess.run(
                    [
                        "ffmpeg", "-v", "error", "-ss", str(seconds), "-i",
                        str(output), "-frames:v", "1", "-vf", "scale=1:1",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                    ],
                    check=True, capture_output=True,
                ).stdout
                return tuple(frame)

            colors = [sample_rgb(time) for time in (0.25, 1.0, 1.9)]
            self.assertEqual(
                [max(range(3), key=color.__getitem__) for color in colors],
                [0, 2, 1],
            )
            self.assertEqual([clip.read_bytes() for clip in clips], source_bytes)

    def test_mixed_media_render_keeps_audio_captions_and_scene_timing(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            clips = [
                directory / "first.jpg",
                directory / "middle.mp4",
                directory / "last.jpg",
            ]
            _make_jpg(clips[0], "red")
            _make_mp4(clips[1], "blue")
            _make_jpg(clips[2], "green")
            source_bytes = [clip.read_bytes() for clip in clips]
            output_dir = directory / "mixed-with-audio"
            output_dir.mkdir()
            audio = directory / "narration.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2.25",
                    "-c:a", "pcm_s16le", str(audio),
                ],
                check=True, capture_output=True,
            )
            scenes = [
                {"text": "First café scene"},
                {"text": "Middle scene"},
                {"text": "Last scene"},
            ]
            timings = [
                {"start": 0, "end": 0.75},
                {"start": 0.75, "end": 1.5},
                {"start": 1.5, "end": 2.25},
            ]
            captions = build_scene_captions(
                scenes, timings, output_dir / "captions.ass",
            )
            output, duration = assemble_media_only(
                clips, output_dir, 12, 0.75,
                audio_path=audio, captions_path=captions,
                image_motion=False, transition="hard_cut",
            )
            balanced_dir = directory / "mixed-balanced"
            balanced_dir.mkdir()
            balanced_captions = build_scene_captions(
                scenes, timings, balanced_dir / "captions.ass",
            )
            balanced_output, balanced_duration = assemble_media_only(
                clips, balanced_dir, 12, 0.75,
                audio_path=audio, captions_path=balanced_captions,
                image_motion=False, transition="hard_cut",
                export_preset="balanced",
            )
            self.assertAlmostEqual(balanced_duration, duration, delta=1 / 12)
            self.assertLess(balanced_output.stat().st_size, output.stat().st_size)

            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-count_frames",
                    "-show_entries",
                    "stream=codec_type,width,height,nb_read_frames:format=duration",
                    "-of", "json", str(output),
                ],
                check=True, capture_output=True, text=True,
            )
            metadata = json.loads(probe.stdout)
            self.assertEqual(
                {stream["codec_type"] for stream in metadata["streams"]},
                {"video", "audio"},
            )
            video = next(stream for stream in metadata["streams"]
                         if stream["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (1080, 1920))
            self.assertEqual(video["nb_read_frames"], "27")
            self.assertAlmostEqual(duration, 2.25, delta=1 / 12)
            self.assertAlmostEqual(
                float(metadata["format"]["duration"]), 2.25, delta=1 / 12,
            )

            def sample_rgb(seconds: float) -> tuple[int, int, int]:
                frame = subprocess.run(
                    [
                        "ffmpeg", "-v", "error", "-ss", str(seconds), "-i",
                        str(output), "-frames:v", "1",
                        "-vf", "crop=1:1:0:0,format=rgb24",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                    ],
                    check=True, capture_output=True,
                ).stdout
                return tuple(frame)

            colors = [sample_rgb(time) for time in (0.25, 1.0, 1.9)]
            self.assertEqual(
                [max(range(3), key=color.__getitem__) for color in colors],
                [0, 2, 1],
            )
            caption_text = captions.read_text(encoding="utf-8")
            self.assertIn("First café scene", caption_text)
            self.assertIn("0:00:00.75", caption_text)
            scene_style = next(
                line for line in caption_text.splitlines()
                if line.startswith("Style: Scene,")
            )
            self.assertTrue(scene_style.endswith(",2,97,97,270,1"))
            self.assertEqual([clip.read_bytes() for clip in clips], source_bytes)

            balanced_probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries",
                    "stream=codec_type,codec_name,width,height:format=duration",
                    "-of", "json", str(balanced_output),
                ],
                check=True, capture_output=True, text=True,
            )
            balanced_metadata = json.loads(balanced_probe.stdout)
            self.assertEqual(
                {stream["codec_type"] for stream in balanced_metadata["streams"]},
                {"video", "audio"},
            )
            self.assertTrue(all(
                stream["codec_name"] in {"h264", "aac"}
                for stream in balanced_metadata["streams"]
            ))
            self.assertAlmostEqual(
                float(balanced_metadata["format"]["duration"]),
                2.25, delta=1 / 12,
            )

    def test_balanced_audio_only_render_stream_copies_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            clips = [directory / "still.jpg", directory / "motion.mp4"]
            _make_jpg(clips[0], "red")
            _make_mp4(clips[1], "blue")
            audio = directory / "narration.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
                    "-c:a", "pcm_s16le", str(audio),
                ],
                check=True, capture_output=True,
            )
            ffmpeg_calls = []

            def record_ffmpeg(args, cwd=None):
                ffmpeg_calls.append(args)
                run_ffmpeg_command(args, cwd)

            with patch("src.assemble.run_ffmpeg", side_effect=record_ffmpeg):
                output, duration = assemble_media_only(
                    clips, directory / "audio-only", 12, 0.75,
                    audio_path=audio, image_motion=False,
                    export_preset="balanced",
                )

            self.assertTrue(any(
                args[index:index + 2] == ["-c:v", "copy"]
                for args in ffmpeg_calls
                for index in range(len(args) - 1)
            ))
            self.assertAlmostEqual(duration, 1.5, delta=1 / 12)
            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-show_entries",
                    "stream=codec_type,codec_name,width,height,pix_fmt,avg_frame_rate:"
                    "format=duration",
                    "-of", "json", str(output),
                ],
                check=True, capture_output=True, text=True,
            )
            info = json.loads(probe.stdout)
            video = next(stream for stream in info["streams"]
                         if stream["codec_type"] == "video")
            audio_stream = next(stream for stream in info["streams"]
                                if stream["codec_type"] == "audio")
            self.assertEqual(video["codec_name"], "h264")
            self.assertEqual(
                (video["width"], video["height"], video["pix_fmt"]),
                (1080, 1920, "yuv420p"),
            )
            self.assertEqual(video["avg_frame_rate"], "12/1")
            self.assertEqual(audio_stream["codec_name"], "aac")
            self.assertAlmostEqual(float(info["format"]["duration"]), 1.5, delta=1 / 12)

    def test_crossfade_overlap_duration_and_timing_are_frame_accurate(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            clips = [
                directory / "red.jpg",
                directory / "blue.mp4",
                directory / "green.jpg",
            ]
            _make_jpg(clips[0], "red")
            _make_mp4(clips[1], "blue")
            _make_jpg(clips[2], "green")
            output, duration = assemble_media_only(
                clips, directory / "crossfade", 12, 1.5,
                image_motion=False, transition="crossfade",
            )
            expected_frames = 3 * 18 - 2 * round(0.4 * 12)
            self.assertEqual(expected_frames, 44)
            self.assertAlmostEqual(duration, expected_frames / 12, places=3)
            self.assertAlmostEqual(ffprobe_duration(output), expected_frames / 12, delta=1 / 12)
            frame_probe = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-count_frames",
                    "-select_streams", "v:0", "-show_entries",
                    "stream=nb_read_frames,r_frame_rate:format=duration",
                    "-of", "json", str(output),
                ],
                check=True, capture_output=True, text=True,
            )
            metadata = json.loads(frame_probe.stdout)
            self.assertEqual(metadata["streams"][0]["nb_read_frames"], "44")
            self.assertEqual(metadata["streams"][0]["r_frame_rate"], "12/1")

            def sample_rgb(seconds: float) -> tuple[int, int, int]:
                frame = subprocess.run(
                    [
                        "ffmpeg", "-v", "error", "-ss", str(seconds), "-i",
                        str(output), "-frames:v", "1", "-vf", "scale=1:1",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                    ],
                    check=True, capture_output=True,
                ).stdout
                return tuple(frame)

            before, first_blend, middle, second_blend, after = [
                sample_rgb(time) for time in (0.5, 1.3, 1.8, 2.35, 3.2)
            ]
            self.assertGreater(before[0], max(before[1:]))
            self.assertGreater(first_blend[0], 20)
            self.assertGreater(first_blend[2], 20)
            self.assertGreater(middle[2], max(middle[:2]))
            self.assertGreater(second_blend[1], 20)
            self.assertGreater(second_blend[2], 20)
            self.assertGreater(after[1], max(after[::2]))

    def test_media_only_render_rejects_excessive_duration_before_queueing(self):
        project_id = uuid.uuid4().hex
        existing_jobs = set(server.STUDIO_RENDER_JOBS)
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            project_dir.mkdir(parents=True)
            (project_dir / "project.json").write_text(json.dumps({
                "id": project_id,
                "name": "Too long",
                "created": "2026-01-01T00:00:00",
                "media": [],
            }), encoding="utf-8")
            with (
                patch.object(server, "STUDIO_PROJECTS", projects_dir),
                patch.object(server, "_validated_project_media",
                             return_value=[Path("unused")] * 21),
            ):
                with self.assertRaises(HTTPException) as excessive:
                    server.render_studio_project(
                        project_id,
                        server.StudioRenderReq(fps=12, seconds_per_scene=30),
                    )
                self.assertEqual(excessive.exception.status_code, 400)
                self.assertIn("600-second", excessive.exception.detail)
                self.assertEqual(set(server.STUDIO_RENDER_JOBS), existing_jobs)

    def test_project_without_script_keeps_media_only_render_path(self):
        project_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            project_dir.mkdir(parents=True)
            media = []
            for index in range(2):
                media_id = uuid.uuid4().hex
                filename = f"{media_id}.png"
                (project_dir / filename).write_bytes(b"fixture")
                media.append({
                    "id": media_id,
                    "name": filename,
                    "extension": ".png",
                    "size": 7,
                    "file": filename,
                })
            (project_dir / "project.json").write_text(json.dumps({
                "id": project_id,
                "name": "Media only",
                "created": "2026-01-01T00:00:00",
                "media": media,
            }), encoding="utf-8")
            called = threading.Event()
            with (
                patch.object(server, "STUDIO_PROJECTS", projects_dir),
                patch.object(server, "_render_studio_project") as worker,
            ):
                worker.side_effect = lambda *args: called.set()
                job = server.render_studio_project(
                    project_id, server.StudioRenderReq(fps=12, seconds_per_scene=2.3)
                )
                self.assertTrue(called.wait(5))
                worker.assert_called_once()
                args = worker.call_args.args
                self.assertEqual([path.name for path in args[1]],
                                 [item["file"] for item in media])
                self.assertIsNone(args[2])
                self.assertEqual(args[0].settings.seconds_per_scene, 2.3)
                server.STUDIO_RENDER_JOBS.pop(job["id"], None)

    def test_three_scene_render_order_duration_and_export_endpoints(self):
        project_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            project_dir.mkdir(parents=True)
            colors = [(240, 20, 20), (20, 230, 20), (20, 20, 240)]
            media = []
            original_bytes = {}
            for color in colors:
                media_id = uuid.uuid4().hex
                filename = f"{media_id}.png"
                media_path = project_dir / filename
                _write_solid_png(media_path, color)
                original_bytes[media_id] = media_path.read_bytes()
                media.append({
                    "id": media_id,
                    "name": f"{color}.png",
                    "extension": ".png",
                    "size": media_path.stat().st_size,
                    "file": filename,
                })

            previous_export = {
                "id": uuid.uuid4().hex,
                "name": "Earlier export",
                "duration": 8.0,
            }
            (project_dir / "project.json").write_text(json.dumps({
                "id": project_id,
                "name": "Timeline integration",
                "created": "2026-01-01T00:00:00",
                "media": media,
                "exports": [previous_export],
                "custom_metadata": {"keep": True},
            }), encoding="utf-8")

            scenes = [
                server.StudioSceneReq(
                    id=uuid.uuid4().hex, text=f"Scene {index + 1}",
                    media_id=media[(index + 2) % len(media)]["id"], duration=duration,
                )
                for index, duration in enumerate((0.75, 1.5, 2.25))
            ]
            with patch.object(server, "STUDIO_PROJECTS", projects_dir):
                saved = server.save_studio_story(
                    project_id, server.StudioStoryReq(
                        script="Scene 1\nScene 2\nScene 3", scenes=scenes
                    )
                )
                self.assertEqual(
                    [scene["text"] for scene in saved["scenes"]],
                    ["Scene 1", "Scene 2", "Scene 3"],
                )
                loaded = server.studio_project(project_id)
                self.assertEqual(loaded["story_script"], "Scene 1\nScene 2\nScene 3")
                self.assertEqual(len(loaded["scenes"]), 3)
                saved_manifest = server._studio_manifest(project_id)
                self.assertEqual(saved_manifest["exports"], [previous_export])
                self.assertEqual(saved_manifest["custom_metadata"], {"keep": True})

                job_data = server.render_studio_project(
                    project_id, server.StudioRenderReq(
                        fps=12, aspect_ratio="16:9",
                    )
                )
                job = server.STUDIO_RENDER_JOBS[job_data["id"]]
                deadline = time.monotonic() + 120
                while job.status in ("queued", "running") and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual(job.status, "completed", job.error)
                export_id = job.export_id
                self.assertIsNotNone(export_id)

                export_path = project_dir / "exports" / export_id / "final.mp4"
                self.assertAlmostEqual(ffprobe_duration(export_path), 4.5, delta=1 / 12)
                dimensions = subprocess.run(
                    [
                        "ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height",
                        "-of", "csv=s=x:p=0", str(export_path),
                    ],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
                self.assertEqual(dimensions, "1920x1080")

                def sample_rgb(seconds: float) -> tuple[int, int, int]:
                    import subprocess

                    frame = subprocess.run(
                        [
                            "ffmpeg", "-v", "error", "-ss", str(seconds), "-i",
                            str(export_path), "-frames:v", "1", "-vf", "scale=1:1",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
                        ],
                        check=True, capture_output=True,
                    ).stdout
                    self.assertEqual(len(frame), 3)
                    return tuple(frame)

                sampled = [
                    sample_rgb(seconds)
                    for seconds in (0.25, 0.65, 0.9, 2.1, 2.4, 3.0)
                ]
                self.assertEqual([max(range(3), key=rgb.__getitem__) for rgb in sampled],
                                 [2, 2, 0, 0, 1, 1])

                preview = server.studio_export_video(project_id, export_id)
                download = server.studio_export_download(project_id, export_id)
                self.assertEqual(Path(preview.path), export_path)
                self.assertEqual(Path(download.path), export_path)
                self.assertEqual(preview.media_type, "video/mp4")
                self.assertEqual(download.media_type, "video/mp4")
                self.assertIn("attachment", download.headers["content-disposition"])
                final_manifest = server._studio_manifest(project_id)
                self.assertEqual(final_manifest["exports"][0], previous_export)
                self.assertEqual(final_manifest["custom_metadata"], {"keep": True})
                self.assertEqual(
                    {item["id"]: (project_dir / item["file"]).read_bytes()
                     for item in media},
                    original_bytes,
                )
            server.STUDIO_RENDER_JOBS.pop(job_data["id"], None)


if __name__ == "__main__":
    unittest.main()
