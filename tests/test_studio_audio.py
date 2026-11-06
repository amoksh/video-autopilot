import json
import re
import struct
import subprocess
import tempfile
import time
import unittest
import uuid
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.captions import _measure_caption_widths, build_captions, build_scene_captions
from src.utils import ffprobe_duration, run_ffmpeg
from webapp import server


def _write_png(path: Path, color=(18, 18, 28)) -> None:
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


def _project(directory: Path, project_id: str, scenes: list[dict],
             audio_settings: dict) -> tuple[dict, bytes]:
    directory.mkdir(parents=True)
    media_id = uuid.uuid4().hex
    filename = f"{media_id}.png"
    media_path = directory / filename
    _write_png(media_path)
    media = {
        "id": media_id, "name": filename, "extension": ".png",
        "size": media_path.stat().st_size, "file": filename,
    }
    previous_export = {"id": uuid.uuid4().hex, "name": "Earlier export"}
    manifest = {
        "id": project_id,
        "name": "Audio integration",
        "created": "2026-01-01T00:00:00",
        "media": [media],
        "story_script": "\n".join(scene["text"] for scene in scenes),
        "scenes": [
            {
                "id": uuid.uuid4().hex, "media_id": media_id,
                "allow_reuse": True, **scene,
            }
            for scene in scenes
        ],
        "audio_settings": audio_settings,
        "exports": [previous_export],
        "custom_metadata": {"preserve": True},
    }
    (directory / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest, media_path.read_bytes()


def _ass_dialogue_text(line: str) -> str:
    return line.split(",", 9)[-1]


_MIDNIGHT_SIGNAL_SCENES = [
    {
        "text": (
            "At midnight, a stranger stepped into a rain-soaked city where every "
            "shadow looked like a witness, every headlight cut through the fog, "
            "and the empty street seemed to be waiting for him."
        ),
        "duration": 6.9,
    },
    {
        "text": (
            "The streets were silent, but somewhere behind the passing cars a "
            "patient watcher followed every turn, keeping just far enough away "
            "to disappear whenever the stranger looked back."
        ),
        "duration": 6.9,
    },
    {
        "text": (
            "A red signal glowed through the rain on a hidden communication "
            "device; it was not a warning to turn around, but a command that "
            "sent the stranger deeper into the night."
        ),
        "duration": 6.9,
    },
    {
        "text": (
            "Far from the avenue, two silent guards received the message and "
            "waited in a dark room; by the time the stranger understood the "
            "trap, every exit had already been watched."
        ),
        "duration": 6.9,
    },
]


class StudioAudioTest(unittest.TestCase):
    def test_voice_validation_and_caption_input_validation(self):
        defaults = server.StudioAudioSettings()
        self.assertEqual(defaults.caption_size, "medium")
        self.assertEqual(defaults.caption_position, "lower")
        with self.assertRaises(ValidationError):
            server.StudioAudioSettings(voice="paid-or-unsupported-voice")
        with self.assertRaises(ValidationError):
            server.StudioAudioSettings(speech_buffer=30)
        with self.assertRaises(ValidationError):
            server.StudioAudioSettings(caption_size="giant")
        with self.assertRaises(ValidationError):
            server.StudioAudioSettings(caption_position="top")

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "captions.ass"
            with self.assertRaisesRegex(ValueError, "cannot be empty"):
                build_scene_captions(
                    [{"text": " \n "}], [{"start": 0, "end": 1}], output,
                )
            with self.assertRaisesRegex(ValueError, "timestamps"):
                build_scene_captions(
                    [{"text": "Caption"}],
                    [{"start": 2, "end": 1}],
                    output,
                )
            with self.assertRaisesRegex(ValueError, "inside their scene"):
                build_scene_captions(
                    [{"text": "Caption"}],
                    [{"start": 0, "caption_end": 1.1, "end": 1}],
                    output,
                )
            path = build_scene_captions(
                [{"text": "Text {\\an8} café"}],
                [{"start": 0, "end": 1}],
                output,
            )
            contents = path.read_text(encoding="utf-8")
            self.assertNotIn("{\\an8}", contents)
            self.assertIn(r"\{\\an8\}", contents)
            self.assertIn("café", contents)

    def test_caption_wrapping_unicode_styles_and_short_scene_validation(self):
        long_text = (
            "Café déjà vu — punctuation?! Keep every word; "
            "日本語の文章と한국어 그리고 العربية، Unicode stays intact. "
            "A very long unbroken token: "
            + "超長い単語" * 24
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "captions.ass"
            with self.assertRaisesRegex(ValueError, "too long to display legibly"):
                build_scene_captions(
                    [{"text": long_text}], [{"start": 0, "end": 0.5}], output,
                )
            path = build_scene_captions(
                [{"text": long_text}],
                [{"start": 0, "end": 12}],
                output,
                size="large",
                position="lower",
            )
            contents = path.read_text(encoding="utf-8")
            dialogue = [line for line in contents.splitlines()
                        if line.startswith("Dialogue:")]
            self.assertGreater(len(dialogue), 1)
            for line in dialogue:
                caption = line.split(",,", 1)[1]
                self.assertLessEqual(caption.count(r"\N"), 1)
            self.assertIn("Café déjà vu", contents)
            self.assertIn("日本語", contents)
            self.assertIn("العربية", contents)
            self.assertIn("超長い単語", contents)
            rendered_text = "".join(
                _ass_dialogue_text(line).replace(r"\N", "")
                for line in contents.splitlines()
                if line.startswith("Dialogue:")
            )
            self.assertEqual("".join(rendered_text.split()),
                             "".join(long_text.split()))
            style = next(line for line in contents.splitlines()
                         if line.startswith("Style: Scene,"))
            self.assertRegex(style, r",76,&H00FFFFFF,")
            self.assertTrue(style.endswith(",2,97,97,270,1"))

            middle = build_scene_captions(
                [{"text": "Short punctuation?!"}],
                [{"start": 0, "end": 0.4}],
                output,
                width=720,
                height=1280,
                size="small",
                position="middle",
            ).read_text(encoding="utf-8")
            middle_style = next(line for line in middle.splitlines()
                                if line.startswith("Style: Scene,"))
            self.assertRegex(middle_style, r",35,&H00FFFFFF,")
            self.assertTrue(middle_style.endswith(",5,65,65,0,1"))

            with self.assertRaisesRegex(ValueError, "Caption size"):
                build_scene_captions(
                    [{"text": "Text"}], [{"start": 0, "end": 1}],
                    output, size="giant",
                )

            for size in ("small", "medium", "large"):
                sized = build_scene_captions(
                    [{"text": "Wide W punctuation?! (really) — measured."}],
                    [{"start": 0, "end": 2}],
                    output,
                    size=size,
                )
                sized_text = [
                    caption_line
                    for line in sized.read_text(encoding="utf-8").splitlines()
                    if line.startswith("Dialogue:")
                    for caption_line in _ass_dialogue_text(line).split(r"\N")
                ]
                style = next(
                    line for line in sized.read_text(encoding="utf-8").splitlines()
                    if line.startswith("Style: Scene,")
                )
                font_size = int(style.split(",")[2])
                self.assertTrue(sized_text)
                self.assertTrue(all(
                    width <= 1080 - 2 * round(1080 * 0.09)
                    for width in _measure_caption_widths(sized_text, font_size)
                ))

    def test_pipeline_word_captions_wrap_with_measured_font_widths(self):
        narration = (
            "THE MIDNIGHT SIGNAL: At midnight, a stranger stepped into a "
            "rain-soaked city where every headlight cut through the fog—"
            "café lights flickered; nothing looked ordinary?!"
        )
        tokens = narration.split()
        transcript_words = [
            SimpleNamespace(word=word, start=index * 0.25, end=(index + 1) * 0.25)
            for index, word in enumerate(tokens)
        ]

        class FakeWhisperModel:
            def __init__(self, *_args, **_kwargs):
                pass

            def transcribe(self, *_args, **_kwargs):
                return [SimpleNamespace(words=transcript_words)], None

        cfg = {
            "captions": {
                "enabled": True,
                "style": {
                    "fontname": "DejaVu Sans",
                    "font_size": 140,
                    "primary_color": "&H00FFFFFF",
                    "highlight_color": "&H0000F2FF",
                    "outline_color": "&H00000000",
                    "outline": 5,
                    "shadow": 2,
                    "position": "middle",
                    "max_words_per_cue": 12,
                    "uppercase": False,
                },
            },
            "video": {
                "width": 1080, "height": 1920, "safe_zone_padding": 220,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            with patch.dict(
                "sys.modules",
                {"faster_whisper": SimpleNamespace(WhisperModel=FakeWhisperModel)},
            ):
                path = build_captions(cfg, output_dir / "voice.wav", output_dir)

            content = path.read_text(encoding="utf-8")
            dialogue = [
                line for line in content.splitlines()
                if line.startswith("Dialogue:")
            ]
            self.assertEqual(len(dialogue), len(tokens))
            style = next(
                line for line in content.splitlines()
                if line.startswith("Style: Base,")
            ).split(",")
            font_size = int(style[2])
            self.assertLessEqual(font_size, round(1080 * 0.08))
            self.assertEqual(int(style[10]), round(1080 * 0.09))
            self.assertEqual(int(style[11]), round(1080 * 0.09))

            safe_width = (
                1080 - 2 * int(style[10])
                - 2 * (int(style[7]) + int(style[8]))
            )
            for line in dialogue:
                plain_text = re.sub(r"\{.*?\}", "", _ass_dialogue_text(line))
                lines = plain_text.replace(r"\N", "\n").splitlines()
                self.assertTrue(all(
                    width <= safe_width
                    for width in _measure_caption_widths(lines, font_size)
                ))
            first_caption = re.sub(r"\{.*?\}", "", _ass_dialogue_text(dialogue[0]))
            self.assertEqual(
                "".join(first_caption.replace(r"\N", " ").split()),
                "".join(" ".join(tokens[:12]).split()),
            )
            self.assertTrue(any("café" in line for line in dialogue))

            burned_video = output_dir / "pipeline-captions.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30:d=1",
                    "-vf", f"subtitles='{path.as_posix()}'",
                    "-frames:v", "30", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(burned_video),
                ],
                check=True, capture_output=True,
            )
            frame = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-ss", "0.1", "-i", str(burned_video),
                    "-frames:v", "1", "-vf", "format=gray", "-f", "rawvideo",
                    "-pix_fmt", "gray", "pipe:1",
                ],
                check=True, capture_output=True,
            ).stdout
            import numpy as np

            image = np.frombuffer(frame, dtype=np.uint8).reshape((1920, 1080))
            ys, xs = np.where(image > 200)
            self.assertGreater(len(xs), 100)
            self.assertGreaterEqual(int(xs.min()), round(1080 * 0.08))
            self.assertLessEqual(int(xs.max()), round(1080 * 0.92))

    def test_empty_script_invalid_settings_and_excessive_timeline_are_rejected(self):
        project_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            manifest, _ = _project(project_dir, project_id, [], {
                "voiceover_enabled": True, "captions_enabled": False,
                "voice": "en-US-AriaNeural", "speech_buffer": 0.5,
            })
            with patch.object(server, "STUDIO_PROJECTS", projects_dir):
                with self.assertRaises(HTTPException) as empty_script:
                    server.render_studio_project(
                        project_id, server.StudioRenderReq(fps=12)
                    )
                self.assertEqual(empty_script.exception.status_code, 400)

                manifest["audio_settings"]["voice"] = "not-a-supported-voice"
                (project_dir / "project.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                with self.assertRaises(HTTPException) as invalid_voice:
                    server.render_studio_project(
                        project_id, server.StudioRenderReq(fps=12)
                    )
                self.assertEqual(invalid_voice.exception.status_code, 400)

                manifest["audio_settings"] = {
                    "voiceover_enabled": False, "captions_enabled": False,
                    "voice": "en-US-AriaNeural", "speech_buffer": 0.5,
                }
                media_id = manifest["media"][0]["id"]
                manifest["story_script"] = "scene\n" * 21
                manifest["scenes"] = [
                    {"id": uuid.uuid4().hex, "text": f"Scene {i}",
                     "media_id": media_id, "duration": 30}
                    for i in range(21)
                ]
                (project_dir / "project.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                with self.assertRaises(HTTPException) as excessive:
                    server.render_studio_project(
                        project_id, server.StudioRenderReq(fps=12)
                    )
                self.assertEqual(excessive.exception.status_code, 400)

    def test_edge_failure_keeps_source_and_previous_exports(self):
        project_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            manifest, source_bytes = _project(project_dir, project_id, [
                {"text": "Narration", "duration": 1.0},
            ], {
                "voiceover_enabled": True, "captions_enabled": False,
                "voice": "en-US-AriaNeural", "speech_buffer": 0.5,
            })
            with patch.object(server, "STUDIO_PROJECTS", projects_dir):
                media = server._validated_project_media(project_id, manifest)
                job = server.StudioRenderJob(
                    uuid.uuid4().hex, project_id, server.StudioRenderReq(fps=12),
                    server.StudioAudioSettings.model_validate(manifest["audio_settings"]),
                )
                with patch.object(server, "synthesize", side_effect=TimeoutError("service timeout")):
                    server._render_studio_project(
                        job, media, [1.0], [{"text": "Narration"}]
                    )
                self.assertEqual(job.status, "failed")
                self.assertIn("Edge TTS failed for scene 1", job.error)
                self.assertIn("service timeout", job.error)
                self.assertEqual((project_dir / manifest["media"][0]["file"]).read_bytes(),
                                 source_bytes)
                saved = server._studio_manifest(project_id)
                self.assertEqual(saved["exports"], manifest["exports"])
                self.assertEqual(saved["custom_metadata"], {"preserve": True})
                self.assertFalse((project_dir / "exports" / f".staging-{job.id}").exists())

    def test_captions_without_voiceover_remain_silent(self):
        project_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            manifest, _ = _project(project_dir, project_id, [
                {"text": "Visible scene caption", "duration": 1.0},
            ], {
                "voiceover_enabled": False, "captions_enabled": True,
                "voice": "en-US-AriaNeural", "speech_buffer": 0.5,
            })
            with patch.object(server, "STUDIO_PROJECTS", projects_dir):
                result = server.render_studio_project(
                    project_id, server.StudioRenderReq(fps=12)
                )
                job = server.STUDIO_RENDER_JOBS[result["id"]]
                deadline = time.monotonic() + 60
                while job.status in ("queued", "running") and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual(job.status, "completed", job.error)
                export_path = project_dir / "exports" / job.export_id / "final.mp4"
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                     "-of", "json", str(export_path)],
                    check=True, capture_output=True, text=True,
                )
                stream_types = {stream["codec_type"]
                                for stream in json.loads(probe.stdout)["streams"]}
                self.assertEqual(stream_types, {"video"})
                captions = (export_path.parent / "captions.ass").read_text(encoding="utf-8")
                self.assertIn("Visible scene caption", captions)
                self.assertEqual(job.scene_timings[0]["start"], 0)
                self.assertEqual(job.scene_timings[0]["end"], 1.0)
            server.STUDIO_RENDER_JOBS.pop(result["id"], None)

    def test_long_narration_reports_missing_unique_video_seconds(self):
        project_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            project_dir.mkdir(parents=True)
            media_id = uuid.uuid4().hex
            video_path = project_dir / f"{media_id}.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=12:d=1",
                    "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(video_path),
                ],
                check=True, capture_output=True,
            )
            manifest = {
                "id": project_id,
                "name": "Narration coverage",
                "created": "2026-01-01T00:00:00",
                "media": [{
                    "id": media_id, "name": video_path.name,
                    "extension": ".mp4", "file": video_path.name,
                    "size": video_path.stat().st_size,
                    "technical_metadata": {"duration_seconds": 1.0},
                }],
                "story_script": "A longer narration.",
                "scenes": [{
                    "id": uuid.uuid4().hex, "text": "A longer narration.",
                    "media_id": media_id, "duration": 0.5,
                    "approved": True, "reviewed_media_id": media_id,
                }],
                "audio_settings": {
                    "voiceover_enabled": True, "captions_enabled": False,
                    "voice": "en-US-AriaNeural", "speech_buffer": 0.5,
                },
            }
            (project_dir / "project.json").write_text(
                json.dumps(manifest), encoding="utf-8",
            )

            def synthetic_voice(_cfg: dict, _text: str, out_dir: Path) -> Path:
                audio = out_dir / "synthetic.wav"
                run_ffmpeg([
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.8",
                    "-c:a", "pcm_s16le", str(audio),
                ])
                return audio

            with (
                patch.object(server, "STUDIO_PROJECTS", projects_dir),
                patch.object(server, "synthesize", side_effect=synthetic_voice),
            ):
                result = server.render_studio_project(
                    project_id, server.StudioRenderReq(fps=12),
                )
                job = server.STUDIO_RENDER_JOBS[result["id"]]
                deadline = time.monotonic() + 30
                while job.status in ("queued", "running") and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(job.status, "failed")
                self.assertIn("unique footage missing", job.error)
                self.assertIn("Scene 1, shot 1", job.error)
                self.assertIn("0.33s", job.error)
                self.assertFalse(
                    (project_dir / "exports" / result["id"] / "final.mp4").exists()
                )
                server.STUDIO_RENDER_JOBS.pop(result["id"], None)

    def test_two_scene_audio_caption_export_and_export_apis(self):
        project_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="va,story's-") as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            manifest, source_bytes = _project(project_dir, project_id, [
                {"text": "First café {\\an8}", "duration": 0.5},
                {"text": "Second caption", "duration": 1.0},
            ], {
                "voiceover_enabled": True, "captions_enabled": True,
                "voice": "en-US-AriaNeural", "speech_buffer": 0.5,
            })

            def synthetic_voice(_cfg: dict, text: str, out_dir: Path) -> Path:
                duration = 0.8 if text.startswith("First") else 0.4
                audio = out_dir / "synthetic.wav"
                frequency = 440 if text.startswith("First") else 660
                run_ffmpeg([
                    "-f", "lavfi", "-i",
                    f"sine=frequency={frequency}:duration={duration}",
                    "-c:a", "pcm_s16le", str(audio),
                ])
                return audio

            with (
                patch.object(server, "STUDIO_PROJECTS", projects_dir),
                patch.object(server, "synthesize", side_effect=synthetic_voice) as tts,
            ):
                saved_story = server.save_studio_story(
                    project_id,
                    server.StudioStoryReq.model_validate({
                        "script": manifest["story_script"],
                        "scenes": manifest["scenes"],
                        "audio_settings": manifest["audio_settings"],
                    }),
                )
                self.assertTrue(saved_story["audio_settings"]["voiceover_enabled"])
                self.assertTrue(server.studio_project(project_id)["audio_settings"]["captions_enabled"])
                job_data = server.render_studio_project(
                    project_id, server.StudioRenderReq(fps=12)
                )
                job = server.STUDIO_RENDER_JOBS[job_data["id"]]
                deadline = time.monotonic() + 120
                while job.status in ("queued", "running") and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual(job.status, "completed", job.error)
                self.assertEqual(tts.call_count, 2)
                self.assertEqual(tts.call_args_list[0].args[1], "First café {\\an8}")
                self.assertEqual(tts.call_args_list[0].args[0]["tts"]["provider"], "edge")

                export_id = job.export_id
                export_path = project_dir / "exports" / export_id / "final.mp4"
                self.assertTrue(export_path.is_file())
                timings = job.as_dict()["scene_timings"]
                self.assertEqual(len(timings), 2)
                self.assertTrue(timings[0]["extended"])
                self.assertAlmostEqual(timings[0]["narration_duration"], 0.8, delta=0.03)
                self.assertAlmostEqual(timings[0]["output_duration"], 4 / 3, delta=0.01)
                self.assertFalse(timings[1]["extended"])
                self.assertAlmostEqual(timings[1]["start"], timings[0]["end"], places=5)
                self.assertAlmostEqual(job.duration, 7 / 3, delta=0.08)
                self.assertAlmostEqual(job.estimated_duration, 7 / 3, delta=0.08)
                self.assertGreater(job.size_bytes, 0)
                self.assertGreater(job.video_bitrate, 0)
                self.assertGreater(job.audio_bitrate, 0)
                self.assertAlmostEqual(ffprobe_duration(export_path), 7 / 3, delta=0.08)

                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                     "-of", "json", str(export_path)],
                    check=True, capture_output=True, text=True,
                )
                stream_types = {stream["codec_type"]
                                for stream in json.loads(probe.stdout)["streams"]}
                self.assertEqual(stream_types, {"video", "audio"})

                ass = (export_path.parent / "captions.ass").read_text(encoding="utf-8")
                self.assertIn("First café", ass)
                self.assertIn("Second caption", ass)
                self.assertNotIn("{\\an8}", ass)
                self.assertIn("0:00:01.33", ass)

                frame = subprocess.run(
                    ["ffmpeg", "-v", "error", "-ss", "0.25", "-i", str(export_path),
                     "-frames:v", "1", "-vf", "crop=1080:420:0:1400,format=gray",
                     "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                    check=True, capture_output=True,
                ).stdout
                self.assertGreater(sum(pixel > 190 for pixel in frame), 100)

                with TestClient(server.app) as client:
                    preview = client.get(
                        f"/api/studio/projects/{project_id}/exports/{export_id}/video"
                    )
                    download = client.get(
                        f"/api/studio/projects/{project_id}/exports/{export_id}/download"
                    )
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(preview.headers["content-type"], "video/mp4")
                self.assertEqual(len(preview.content), export_path.stat().st_size)
                self.assertEqual(download.status_code, 200)
                self.assertEqual(download.content, preview.content)
                self.assertIn("attachment", download.headers["content-disposition"])

                saved = server._studio_manifest(project_id)
                self.assertEqual(saved["exports"][0], manifest["exports"][0])
                self.assertEqual(saved["custom_metadata"], {"preserve": True})
                self.assertEqual((project_dir / manifest["media"][0]["file"]).read_bytes(),
                                 source_bytes)
            server.STUDIO_RENDER_JOBS.pop(job_data["id"], None)

    def test_two_scene_long_caption_vertical_export_and_audio_level(self):
        project_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = Path(temporary) / "projects" / project_id
            scenes = [
                {
                    "text": (
                        "Scene one: Café déjà vu—can you believe it?! "
                        "Every punctuation mark and multilingual word must remain "
                        "visible in these deliberately long subtitles. "
                        "日本語の文章も最後まで表示されます。"
                    ),
                    "duration": 5.0,
                },
                {
                    "text": (
                        "Scene two: A second long caption keeps its own timing; "
                        "naïve façade, ¿qué tal?, Ελληνικά, العربية, "
                        "and 漢字 are preserved without clipping or omission!"
                    ),
                    "duration": 5.0,
                },
            ]
            manifest, _ = _project(project_dir, project_id, scenes, {
                "voiceover_enabled": True, "captions_enabled": True,
                "voice": "en-US-AriaNeural", "speech_buffer": 0.5,
                "caption_size": "medium", "caption_position": "lower",
            })

            def synthetic_voice(_cfg: dict, text: str, out_dir: Path) -> Path:
                audio = out_dir / "synthetic.wav"
                frequency = 440 if text.startswith("Scene one") else 660
                run_ffmpeg([
                    "-f", "lavfi", "-i",
                    f"sine=frequency={frequency}:duration=4.2",
                    "-c:a", "pcm_s16le", str(audio),
                ])
                return audio

            with (
                patch.object(server, "STUDIO_PROJECTS", projects_dir),
                patch.object(server, "synthesize", side_effect=synthetic_voice),
            ):
                saved_story = server.save_studio_story(
                    project_id,
                    server.StudioStoryReq.model_validate({
                        "script": manifest["story_script"],
                        "scenes": manifest["scenes"],
                        "audio_settings": manifest["audio_settings"],
                    }),
                )
                self.assertEqual(saved_story["audio_settings"]["caption_size"], "medium")
                self.assertEqual(saved_story["audio_settings"]["caption_position"], "lower")
                job_data = server.render_studio_project(
                    project_id, server.StudioRenderReq(fps=12)
                )
                job = server.STUDIO_RENDER_JOBS[job_data["id"]]
                deadline = time.monotonic() + 120
                while job.status in ("queued", "running") and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual(job.status, "completed", job.error)
                self.assertEqual([timing["scene"] for timing in job.scene_timings], [1, 2])
                self.assertAlmostEqual(job.scene_timings[0]["start"], 0, places=5)
                self.assertAlmostEqual(job.scene_timings[1]["start"], 5, places=5)
                self.assertAlmostEqual(job.duration, 10, delta=0.1)

                export_path = project_dir / "exports" / job.export_id / "final.mp4"
                self.assertAlmostEqual(ffprobe_duration(export_path), 10, delta=0.1)
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                     "-of", "json", str(export_path)],
                    check=True, capture_output=True, text=True,
                )
                stream_types = {stream["codec_type"]
                                for stream in json.loads(probe.stdout)["streams"]}
                self.assertEqual(stream_types, {"video", "audio"})
                level_check = subprocess.run(
                    ["ffmpeg", "-v", "info", "-i", str(export_path), "-map", "0:a:0",
                     "-af", "volumedetect", "-f", "null", "-"],
                    check=True, capture_output=True, text=True,
                )
                peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", level_check.stderr)
                self.assertIsNotNone(peak, level_check.stderr[-1000:])
                self.assertGreater(float(peak.group(1)), -30)
                self.assertLessEqual(float(peak.group(1)), -1.5)
                ass = (export_path.parent / "captions.ass").read_text(encoding="utf-8")
                self.assertIn("日本語", ass)
                self.assertIn("العربية", ass)
                self.assertIn("Style: Scene,DejaVu Sans,60,", ass)
                self.assertIn(",2,97,97,270,1", ass)
                dialogues = [line for line in ass.splitlines()
                             if line.startswith("Dialogue:")]
                self.assertGreaterEqual(len(dialogues), 4)
                self.assertTrue(all(_ass_dialogue_text(line).count(r"\N") <= 1
                                    for line in dialogues))

                frame = subprocess.run(
                    ["ffmpeg", "-v", "error", "-ss", "0.3", "-i", str(export_path),
                     "-frames:v", "1", "-vf", "format=gray", "-f", "rawvideo",
                     "-pix_fmt", "gray", "pipe:1"],
                    check=True, capture_output=True,
                ).stdout
                self.assertEqual(len(frame), 1080 * 1920)
                bright_rows = [
                    row for row in range(1920)
                    if max(frame[row * 1080:(row + 1) * 1080]) > 220
                ]
                self.assertTrue(bright_rows, "burned caption pixels should be visible")
                self.assertGreaterEqual(min(bright_rows), 1400)
                self.assertLessEqual(max(bright_rows), 1700)
                self.assertTrue(any(line.startswith("Dialogue: 0,0:00:05.")
                                    for line in dialogues))
            server.STUDIO_RENDER_JOBS.pop(job_data["id"], None)

    def test_midnight_signal_30fps_caption_safe_area_render(self):
        project_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            manifest, _ = _project(project_dir, project_id, _MIDNIGHT_SIGNAL_SCENES, {
                "voiceover_enabled": True, "captions_enabled": True,
                "voice": "en-US-AriaNeural", "speech_buffer": 0.1,
                "caption_size": "medium", "caption_position": "lower",
            })
            synthesized = []

            def synthetic_voice(_cfg: dict, _text: str, out_dir: Path) -> Path:
                audio = out_dir / "synthetic.wav"
                run_ffmpeg([
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=5.2",
                    "-c:a", "pcm_s16le", str(audio),
                ])
                synthesized.append(audio)
                return audio

            with (
                patch.object(server, "STUDIO_PROJECTS", projects_dir),
                patch.object(server, "synthesize", side_effect=synthetic_voice),
            ):
                job_data = server.render_studio_project(
                    project_id, server.StudioRenderReq(fps=30)
                )
                job = server.STUDIO_RENDER_JOBS[job_data["id"]]
                deadline = time.monotonic() + 180
                while job.status in ("queued", "running") and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertEqual(job.status, "completed", job.error)
                self.assertEqual(len(synthesized), 4)
                self.assertAlmostEqual(job.duration, 27.6, delta=0.1)
                for timing, expected_start in zip(
                    job.scene_timings, [0, 6.9, 13.8, 20.7]
                ):
                    self.assertAlmostEqual(
                        timing["start"], expected_start, places=5,
                    )

                export_path = project_dir / "exports" / job.export_id / "final.mp4"
                probe = subprocess.run(
                    [
                        "ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,width,height,r_frame_rate:format=duration",
                        "-of", "json", str(export_path),
                    ],
                    check=True, capture_output=True, text=True,
                )
                video_info = json.loads(probe.stdout)
                video = next(
                    stream for stream in video_info["streams"]
                    if stream["codec_type"] == "video"
                )
                self.assertEqual((video["width"], video["height"]), (1080, 1920))
                self.assertEqual(video["r_frame_rate"], "30/1")
                self.assertTrue(any(
                    stream["codec_type"] == "audio"
                    for stream in video_info["streams"]
                ))
                self.assertAlmostEqual(
                    float(video_info["format"]["duration"]), 27.6, delta=0.15,
                )

                ass = (export_path.parent / "captions.ass").read_text(encoding="utf-8")
                dialogues = [
                    line for line in ass.splitlines()
                    if line.startswith("Dialogue:")
                ]
                self.assertGreater(len(dialogues), len(_MIDNIGHT_SIGNAL_SCENES))
                scene_bounds = [
                    (timing["start"], timing["caption_end"])
                    for timing in job.scene_timings
                ]
                sample_frames = []
                frame_indexes = []
                for scene_index, (start, end) in enumerate(scene_bounds):
                    scene_dialogues = [
                        line for line in dialogues
                        if start <= self._ass_time_seconds(line.split(",", 9)[1]) < end
                    ]
                    self.assertGreater(len(scene_dialogues), 1)
                    for line in scene_dialogues:
                        fields = line.split(",", 9)
                        card_start = self._ass_time_seconds(fields[1])
                        card_end = self._ass_time_seconds(fields[2])
                        self.assertGreaterEqual(card_start, start)
                        self.assertLessEqual(card_end, end + 0.01)
                        sample_frames.append((card_start + card_end) / 2)
                    self.assertEqual(
                        self._ass_time_seconds(scene_dialogues[-1].split(",", 9)[2]),
                        round(end, 2),
                    )

                frame_indexes = [int(seconds * 30) for seconds in sample_frames]
                select = "+".join(f"eq(n\\,{index})" for index in frame_indexes)
                frames = subprocess.run(
                    [
                        "ffmpeg", "-v", "error", "-i", str(export_path),
                        "-vf", f"select='{select}',format=gray", "-vsync", "0",
                        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
                    ],
                    check=True, capture_output=True,
                ).stdout
                import numpy as np

                frame_size = 1080 * 1920
                self.assertEqual(len(frames), frame_size * len(sample_frames))
                measured_bounds = []
                for index in range(len(sample_frames)):
                    image = np.frombuffer(
                        frames[index * frame_size:(index + 1) * frame_size],
                        dtype=np.uint8,
                    ).reshape((1920, 1080))
                    ys, xs = np.where(image > 200)
                    self.assertGreater(len(xs), 100, f"caption frame {index} is not visible")
                    self.assertGreaterEqual(int(xs.min()), round(1080 * 0.08))
                    self.assertLessEqual(int(xs.max()), round(1080 * 0.92))
                    self.assertGreaterEqual(int(ys.min()), round(1920 * 0.70))
                    self.assertLessEqual(int(ys.max()), 1920 - 240)
                    measured_bounds.append((
                        int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()),
                    ))
                print(
                    "THE MIDNIGHT SIGNAL bright-glyph bounds across "
                    f"{len(measured_bounds)} caption frames: "
                    f"x={min(bounds[0] for bounds in measured_bounds)}.."
                    f"{max(bounds[2] for bounds in measured_bounds)}, "
                    f"y={min(bounds[1] for bounds in measured_bounds)}.."
                    f"{max(bounds[3] for bounds in measured_bounds)}"
                )
            server.STUDIO_RENDER_JOBS.pop(job_data["id"], None)

    @staticmethod
    def _ass_time_seconds(value: str) -> float:
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


if __name__ == "__main__":
    unittest.main()
