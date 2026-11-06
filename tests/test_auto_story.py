import json
import struct
import subprocess
import tempfile
import time
import unittest
import uuid
import zlib
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.studio_auto_story import build_auto_story_plan, parse_master_story_brief
from webapp import server


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    width = height = 48
    raw = b"".join(b"\0" + bytes(color) * width for _ in range(height))

    def chunk(name: bytes, data: bytes) -> bytes:
        body = name + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _make_mp4(path: Path, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=64x64:r=12:d=10",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True,
    )


_FOUR_SCENE_BRIEF = """1. Asset: 3
Narration: At midnight, a stranger arrived in a city where every shadow seemed to be watching.
Visual direction: Show the lonely figure standing on a rain-soaked street surrounded by cars and headlights.
Mood: Suspenseful.

2. Asset: 4
Narration: The streets were silent. But someone was already following his every move.
Visual direction: Show silhouettes moving through the rainy city streets.
Mood: Tense.

3. Asset: 2
Narration: Then a mysterious red signal appeared. It wasn't a warning. It was a command.
Visual direction: Show the glowing red communication device in the rain.
Mood: Mysterious.

4. Asset: 1
Narration: Far away, two silent guards received the message. The stranger had walked into their trap.
Visual direction: Show two suited Doberman guards in a dark secret location.
Mood: Ominous."""


class AutoStoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.projects = Path(self.temporary.name) / "projects"
        self.projects.mkdir()
        self.projects_patch = patch.object(server, "STUDIO_PROJECTS", self.projects)
        self.projects_patch.start()
        self.old_jobs = server.STUDIO_RENDER_JOBS.copy()
        server.STUDIO_RENDER_JOBS.clear()
        self.client = TestClient(server.app)
        response = self.client.post(
            "/api/studio/projects", json={"name": "Auto Story test"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.project_id = response.json()["id"]

    def tearDown(self):
        self.client.close()
        server.STUDIO_RENDER_JOBS.clear()
        server.STUDIO_RENDER_JOBS.update(self.old_jobs)
        self.projects_patch.stop()
        self.temporary.cleanup()

    def upload_mixed_assets(self) -> list[dict]:
        assets = [
            ("01-station.png", "image/png", _png_bytes((30, 45, 70))),
            ("02-train.mp4", "video/mp4", None),
            ("03-garden.png", "image/png", _png_bytes((35, 90, 45))),
            ("04-sunset.mp4", "video/mp4", None),
        ]
        uploaded = []
        with tempfile.TemporaryDirectory() as temporary:
            for name, content_type, data in assets:
                if data is None:
                    path = Path(temporary) / name
                    _make_mp4(path, "blue" if name.startswith("02") else "orange")
                    data = path.read_bytes()
                response = self.client.post(
                    f"/api/studio/projects/{self.project_id}/media",
                    files={"file": (name, data, content_type)},
                )
                self.assertEqual(response.status_code, 200, response.text)
                uploaded.append(response.json())
        return uploaded

    def create_request(self, brief: str, **overrides) -> dict:
        request = {
            "master_story_brief": brief,
            "use_upload_order_fallback": False,
            "audio_settings": {
                "voiceover_enabled": False,
                "captions_enabled": False,
                "voice": "en-US-AriaNeural",
                "speech_buffer": 0.5,
                "caption_size": "medium",
                "caption_position": "lower",
            },
            "render_settings": {
                "aspect_ratio": "9:16",
                "fps": 12,
                "seconds_per_scene": 0.5,
            },
        }
        request.update(overrides)
        return request

    def test_quality_settings_persist_and_legacy_projects_default_safely(self):
        created = self.client.get(f"/api/studio/projects/{self.project_id}")
        self.assertEqual(
            created.json()["quality_settings"]["export_preset"], "balanced",
        )
        self.assertEqual(created.json()["aspect_ratio"], "9:16")
        self.assertEqual(
            (created.json()["width"], created.json()["height"]),
            (1080, 1920),
        )
        manifest = server._studio_manifest(self.project_id)
        manifest.pop("quality_settings", None)
        server._save_studio_manifest(self.project_id, manifest)

        legacy = self.client.get(f"/api/studio/projects/{self.project_id}")
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(
            legacy.json()["quality_settings"],
            {
                "aspect_ratio": "9:16",
                "image_motion": True,
                "transition": "hard_cut",
                "export_preset": "legacy",
            },
        )

        saved = self.client.put(
            f"/api/studio/projects/{self.project_id}/quality-settings",
            json={
                "aspect_ratio": "16:9",
                "image_motion": False,
                "transition": "crossfade",
                "export_preset": "high_quality",
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        reloaded = self.client.get(f"/api/studio/projects/{self.project_id}")
        self.assertEqual(
            reloaded.json()["quality_settings"],
            {
                "aspect_ratio": "16:9",
                "image_motion": False,
                "transition": "crossfade",
                "export_preset": "high_quality",
            },
        )
        self.assertEqual(reloaded.json()["aspect_ratio"], "16:9")
        self.assertEqual(
            (reloaded.json()["width"], reloaded.json()["height"]),
            (1920, 1080),
        )
        legacy_update = self.client.put(
            f"/api/studio/projects/{self.project_id}/quality-settings",
            json={"image_motion": True, "transition": "hard_cut"},
        )
        self.assertEqual(
            legacy_update.json()["quality_settings"]["export_preset"],
            "high_quality",
        )
        self.assertEqual(
            legacy_update.json()["quality_settings"]["aspect_ratio"],
            "16:9",
        )

    def test_manual_auto_director_and_auto_story_use_saved_output_profile(self):
        self.upload_mixed_assets()
        with patch.object(server.threading, "Thread") as worker:
            worker.return_value.start.return_value = None
            manual = self.client.post(
                f"/api/studio/projects/{self.project_id}/render",
                json={
                    "aspect_ratio": "16:9",
                    "fps": 12,
                    "seconds_per_scene": 0.5,
                    "export_preset": "balanced",
                },
            )
            self.assertEqual(manual.status_code, 202, manual.text)
            self.assertEqual(
                (manual.json()["width"], manual.json()["height"]), (1920, 1080),
            )
            self.assertEqual(manual.json()["aspect_ratio"], "16:9")
            server.STUDIO_RENDER_JOBS.pop(manual.json()["id"], None)

            auto_director = self.client.post(
                f"/api/studio/projects/{self.project_id}/auto-director/create",
                json={
                    "aspect_ratio": "16:9",
                    "fps": 12,
                    "seconds_per_scene": 0.5,
                    "export_preset": "balanced",
                },
            )
            self.assertEqual(auto_director.status_code, 202, auto_director.text)
            self.assertEqual(auto_director.json()["status"], "rendering")
            self.assertEqual(auto_director.json()["job"]["aspect_ratio"], "16:9")
            self.assertEqual(
                (auto_director.json()["job"]["width"],
                 auto_director.json()["job"]["height"]),
                (1920, 1080),
            )
            server.STUDIO_RENDER_JOBS.pop(auto_director.json()["job"]["id"], None)

            auto_story = self.client.post(
                f"/api/studio/projects/{self.project_id}/auto-story/create",
                json=self.create_request(
                    "1. Asset: 1\nNarration: A landscape scene.",
                    render_settings={
                        "aspect_ratio": "16:9",
                        "fps": 12,
                        "seconds_per_scene": 0.5,
                        "export_preset": "balanced",
                    },
                ),
            )
            self.assertEqual(auto_story.status_code, 202, auto_story.text)
            self.assertEqual(auto_story.json()["status"], "rendering")
            self.assertEqual(auto_story.json()["job"]["aspect_ratio"], "16:9")
            self.assertEqual(
                (auto_story.json()["job"]["width"],
                 auto_story.json()["job"]["height"]),
                (1920, 1080),
            )
            self.assertEqual(
                server._studio_manifest(self.project_id)["quality_settings"]["aspect_ratio"],
                "16:9",
            )
            server.STUDIO_RENDER_JOBS.pop(auto_story.json()["job"]["id"], None)

    def test_adaptive_pacing_setting_persists_and_legacy_input_defaults_off(self):
        project = self.client.get(
            f"/api/studio/projects/{self.project_id}"
        ).json()
        self.assertFalse(project["auto_mode_settings"]["adaptive_pacing"])
        saved = self.client.put(
            f"/api/studio/projects/{self.project_id}/auto-story/input",
            json={
                "master_story_brief": "1. A measured scene.",
                "adaptive_pacing": True,
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertTrue(saved.json()["auto_mode_settings"]["adaptive_pacing"])

        legacy_update = self.client.put(
            f"/api/studio/projects/{self.project_id}/auto-story/input",
            json={"master_story_brief": "1. Still the same scene."},
        )
        self.assertTrue(legacy_update.json()["auto_mode_settings"]["adaptive_pacing"])

    def test_crossfade_is_rejected_when_voiceover_would_be_desynchronized(self):
        media = self.upload_mixed_assets()
        scene = {
            "id": uuid.uuid4().hex,
            "text": "A spoken scene.",
            "media_id": media[0]["id"],
            "duration": 1.0,
            "approved": True,
            "reviewed_media_id": media[0]["id"],
        }
        saved = self.client.put(
            f"/api/studio/projects/{self.project_id}/story",
            json={
                "script": scene["text"],
                "scenes": [scene],
                "audio_settings": {
                    "voiceover_enabled": True,
                    "captions_enabled": False,
                    "voice": "en-US-AriaNeural",
                    "speech_buffer": 0.5,
                    "caption_size": "medium",
                    "caption_position": "lower",
                },
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        response = self.client.post(
            f"/api/studio/projects/{self.project_id}/render",
            json={
                "fps": 12,
                "seconds_per_scene": 1,
                "image_motion": False,
                "transition": "crossfade",
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("desynchronize", response.json()["detail"])

    def test_crossfade_is_rejected_for_explicit_action_scenes(self):
        media = self.upload_mixed_assets()
        scene = {
            "id": uuid.uuid4().hex,
            "text": "The signal appears.",
            "media_id": media[0]["id"],
            "duration": 1.0,
            "intended_action": "A sudden signal flash must remain clear.",
            "approved": True,
            "reviewed_media_id": media[0]["id"],
        }
        saved = self.client.put(
            f"/api/studio/projects/{self.project_id}/story",
            json={"script": scene["text"], "scenes": [scene]},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        response = self.client.post(
            f"/api/studio/projects/{self.project_id}/render",
            json={
                "fps": 12,
                "seconds_per_scene": 1,
                "image_motion": False,
                "transition": "crossfade",
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("action boundaries", response.json()["detail"])

    def test_story_parser_applies_explicit_order_and_exact_filename_mapping(self):
        parsed = parse_master_story_brief(
            "Characters: Mira\nLocation: Old harbor\n"
            "Ordering: Scene 2, Scene 1\n"
            "Scene 1: First beat | Narration: First line. | File: harbor.png\n"
            "Scene 2: Second beat | Narration: Second line. | Asset: 2"
        )
        self.assertTrue(parsed["ordering_applied"])
        self.assertEqual([scene["number"] for scene in parsed["scenes"]], [2, 1])
        self.assertEqual(parsed["scenes"][1]["characters"], ["Mira"])
        media = [
            {"id": uuid.uuid4().hex, "name": "harbor.png"},
            {"id": uuid.uuid4().hex, "name": "bridge.mp4"},
        ]
        plan = build_auto_story_plan(parsed, media, 2.0, False)
        self.assertTrue(plan["ready"], plan["errors"])
        self.assertEqual(
            [scene["media_id"] for scene in plan["scenes"]],
            [media[1]["id"], media[0]["id"]],
        )
        self.assertEqual(
            [scene["match_basis"] for scene in plan["scenes"]],
            ["explicit mapping", "explicit mapping"],
        )

        duplicates = build_auto_story_plan(
            parse_master_story_brief("1. First.\n1. Repeated."),
            media, 2.0, True,
        )
        self.assertFalse(duplicates["ready"])
        self.assertTrue(any("repeated" in error for error in duplicates["errors"]))
        paragraphs = parse_master_story_brief(
            "Story: First sentence. Second sentence.\n\n"
            "A new paragraph follows."
        )
        self.assertEqual(
            [scene["text"] for scene in paragraphs["scenes"]],
            ["First sentence.", "Second sentence.", "A new paragraph follows."],
        )

    def test_exact_four_scene_brief_extracts_narration_directions_and_assets(self):
        parsed = parse_master_story_brief(_FOUR_SCENE_BRIEF)
        expected = [
            (
                "At midnight, a stranger arrived in a city where every shadow seemed to be watching.",
                "Show the lonely figure standing on a rain-soaked street surrounded by cars and headlights.",
                "Suspenseful.",
                [3],
            ),
            (
                "The streets were silent. But someone was already following his every move.",
                "Show silhouettes moving through the rainy city streets.",
                "Tense.",
                [4],
            ),
            (
                "Then a mysterious red signal appeared. It wasn't a warning. It was a command.",
                "Show the glowing red communication device in the rain.",
                "Mysterious.",
                [2],
            ),
            (
                "Far away, two silent guards received the message. The stranger had walked into their trap.",
                "Show two suited Doberman guards in a dark secret location.",
                "Ominous.",
                [1],
            ),
        ]
        self.assertEqual(
            [
                (scene["text"], scene["visual_description"], scene["mood"],
                 scene["asset_numbers"])
                for scene in parsed["scenes"]
            ],
            expected,
        )
        self.assertTrue(all(scene["text"].strip() for scene in parsed["scenes"]))

    def test_prior_two_scene_brief_and_empty_narration_persistence(self):
        first = self.client.post(
            f"/api/studio/projects/{self.project_id}/media",
            files={"file": ("asset-1.png", _png_bytes((15, 25, 35)), "image/png")},
        ).json()
        second = self.client.post(
            f"/api/studio/projects/{self.project_id}/media",
            files={"file": ("asset-2.png", _png_bytes((35, 25, 15)), "image/png")},
        ).json()
        two_scene_brief = """1. Asset: 2
Narration: The streets were silent. Someone followed close behind.
Visual direction: Show a quiet city street at night.
Mood: Tense.

2. Asset: 1
Visual direction: Show the stranger looking back.
Narration: He turned, but no one was there.
Mood: Suspenseful."""
        two_scene_plan = build_auto_story_plan(
            parse_master_story_brief(two_scene_brief),
            [
                {"id": first["id"], "name": first["name"]},
                {"id": second["id"], "name": second["name"]},
            ],
            1.0,
            False,
        )
        self.assertTrue(two_scene_plan["ready"], two_scene_plan["errors"])
        self.assertEqual(
            [scene["text"] for scene in two_scene_plan["scenes"]],
            [
                "The streets were silent. Someone followed close behind.",
                "He turned, but no one was there.",
            ],
        )
        self.assertEqual(
            [scene["media_id"] for scene in two_scene_plan["scenes"]],
            [second["id"], first["id"]],
        )

        old_scene = {
            "id": uuid.uuid4().hex,
            "text": "Previously valid narration.",
            "media_id": first["id"],
            "reviewed_media_id": None,
            "duration": 1.0,
            "visual_description": "Previously saved direction.",
            "characters": [],
            "location": "",
            "mood": "",
            "intended_action": "",
            "assignment_source": "manual",
            "approved": False,
        }
        saved = self.client.put(
            f"/api/studio/projects/{self.project_id}/story",
            json={
                "script": old_scene["text"],
                "scenes": [old_scene],
                "audio_settings": {},
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        manifest_path = self.projects / self.project_id / "project.json"
        previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        empty_narration_brief = """1. Asset: 1
Narration:
Visual direction: Show only this visual.
Mood: Quiet."""
        empty_result = self.client.post(
            f"/api/studio/projects/{self.project_id}/auto-story/create",
            json=self.create_request(empty_narration_brief),
        )
        self.assertEqual(empty_result.status_code, 202, empty_result.text)
        self.assertEqual(empty_result.json()["status"], "needs_input")
        self.assertEqual(empty_result.json()["plan"][0]["text"], "")
        self.assertTrue(any(
            "missing narration or scene text" in message
            for message in empty_result.json()["input_required"]
        ))
        current_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(current_manifest["scenes"], previous_manifest["scenes"])
        self.assertEqual(current_manifest["story_script"], previous_manifest["story_script"])
        self.assertFalse(server.STUDIO_RENDER_JOBS)

        with patch.object(server, "render_studio_project", return_value={"id": "test-job"}) as start_render:
            preview = self.client.post(
                f"/api/studio/projects/{self.project_id}/auto-story/create",
                json=self.create_request(two_scene_brief, preview_only=True),
            )
            self.assertEqual(preview.status_code, 202, preview.text)
            self.assertEqual(preview.json()["status"], "preview")
            self.assertEqual(
                [scene["text"] for scene in preview.json()["plan"]],
                [
                    "The streets were silent. Someone followed close behind.",
                    "He turned, but no one was there.",
                ],
            )
            self.assertEqual(
                [scene["asset_name"] for scene in preview.json()["plan"]],
                ["asset-2.png", "asset-1.png"],
            )
            self.assertFalse(start_render.called)
            current_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(current_manifest["scenes"], previous_manifest["scenes"])
            self.assertEqual(current_manifest["story_script"], previous_manifest["story_script"])

            response = self.client.post(
                f"/api/studio/projects/{self.project_id}/auto-story/create",
                json=self.create_request(two_scene_brief),
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["status"], "rendering")
            self.assertTrue(start_render.called)
            saved_at_render_start = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [scene["text"] for scene in saved_at_render_start["scenes"]],
                [
                    "The streets were silent. Someone followed close behind.",
                    "He turned, but no one was there.",
                ],
            )

        other_project = self.client.post(
            "/api/studio/projects", json={"name": "Isolated Auto Story project"},
        ).json()
        isolated = self.client.get(
            f"/api/studio/projects/{other_project['id']}"
        ).json()
        self.assertEqual(isolated["scenes"], [])
        self.assertEqual(isolated["media"], [])
        self.assertEqual(
            self.client.get(
                f"/api/studio/media/{other_project['id']}/{first['id']}"
            ).status_code,
            404,
        )

    def test_four_scene_mixed_asset_export_voiceover_captions_and_old_export(self):
        uploaded = self.upload_mixed_assets()
        prior_export_id = uuid.uuid4().hex
        prior_dir = self.projects / self.project_id / "exports" / prior_export_id
        prior_dir.mkdir(parents=True)
        prior_video = prior_dir / "final.mp4"
        prior_bytes = b"previous export is immutable"
        prior_video.write_bytes(prior_bytes)
        manifest_path = self.projects / self.project_id / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["exports"] = [{
            "id": prior_export_id,
            "name": "Earlier export",
            "created": "2026-01-01T00:00:00",
            "duration": 1.0,
        }]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        audio_settings = {
            "voiceover_enabled": True,
            "captions_enabled": True,
            "voice": "en-US-GuyNeural",
            "speech_buffer": 0.1,
            "caption_size": "small",
            "caption_position": "middle",
        }
        input_response = self.client.put(
            f"/api/studio/projects/{self.project_id}/auto-story/input",
            json={
                "master_story_brief": _FOUR_SCENE_BRIEF,
                "use_upload_order_fallback": False,
                "adaptive_pacing": True,
                "audio_settings": audio_settings,
            },
        )
        self.assertEqual(input_response.status_code, 200, input_response.text)

        def stub_synthesize(cfg: dict, text: str, out_dir: Path) -> Path:
            voice = out_dir / "voice.mp3"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=880:duration=2.2",
                    "-q:a", "9", str(voice),
                ],
                check=True, capture_output=True,
            )
            return voice

        render_start_snapshots = []
        real_render = server.render_studio_project

        def verify_saved_scenes_then_start(project_id: str, settings):
            manifest_at_start = json.loads(
                (self.projects / project_id / "project.json").read_text(
                    encoding="utf-8"
                )
            )
            render_start_snapshots.append(manifest_at_start["scenes"])
            return real_render(project_id, settings)

        with (
            patch.object(server, "synthesize", side_effect=stub_synthesize),
            patch.object(
                server, "render_studio_project",
                side_effect=verify_saved_scenes_then_start,
            ),
        ):
            preview_response = self.client.post(
                f"/api/studio/projects/{self.project_id}/auto-story/create",
                json=self.create_request(
                    _FOUR_SCENE_BRIEF, audio_settings=audio_settings,
                    adaptive_pacing=True,
                    preview_only=True,
                ),
            )
            self.assertEqual(preview_response.status_code, 202, preview_response.text)
            preview = preview_response.json()
            self.assertEqual(preview["status"], "preview")
            self.assertEqual(
                [row["text"] for row in preview["plan"]],
                [scene["text"] for scene in parse_master_story_brief(
                    _FOUR_SCENE_BRIEF
                )["scenes"]],
            )
            self.assertEqual(
                [row["asset_name"] for row in preview["plan"]],
                [uploaded[index]["name"] for index in (2, 3, 1, 0)],
            )
            self.assertFalse(server.STUDIO_RENDER_JOBS)
            self.assertEqual(
                json.loads(manifest_path.read_text(encoding="utf-8")).get("scenes", []),
                [],
            )

            response = self.client.post(
                f"/api/studio/projects/{self.project_id}/auto-story/create",
                json=self.create_request(
                    _FOUR_SCENE_BRIEF, audio_settings=audio_settings,
                    adaptive_pacing=True,
                    render_settings={
                        "aspect_ratio": "16:9",
                        "fps": 12,
                        "seconds_per_scene": 0.5,
                    },
                ),
            )
            self.assertEqual(response.status_code, 202, response.text)
            result = response.json()
            self.assertEqual(result["status"], "rendering")
            self.assertEqual(
                [row["asset_name"] for row in result["plan"]],
                [uploaded[index]["name"] for index in (2, 3, 1, 0)],
            )
            self.assertTrue(all(row["basis"] == "explicit mapping" for row in result["plan"]))
            self.assertEqual(len(render_start_snapshots), 1)
            self.assertEqual(
                [scene["text"] for scene in render_start_snapshots[0]],
                [scene["text"] for scene in parse_master_story_brief(
                    _FOUR_SCENE_BRIEF
                )["scenes"]],
            )

            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                status_response = self.client.get(
                    f"/api/studio/projects/{self.project_id}/render"
                )
                job = status_response.json()
                if job["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.2)

        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(job["stage"], "completed")
        self.assertEqual(len(job["scene_timings"]), 4)
        self.assertTrue(all(timing["extended"] for timing in job["scene_timings"]))
        self.assertTrue(all(
            timing["adaptive_recommended_duration"] == 2.5
            and timing["output_duration"] == 2.5
            for timing in job["scene_timings"]
        ))
        self.assertAlmostEqual(
            job["scene_timings"][0]["narration_duration"], 2.2, delta=0.05,
        )
        project = self.client.get(
            f"/api/studio/projects/{self.project_id}"
        ).json()
        self.assertEqual(project["master_story_brief"], _FOUR_SCENE_BRIEF)
        self.assertEqual(project["aspect_ratio"], "16:9")
        self.assertEqual(project["audio_settings"], audio_settings)
        self.assertFalse(project["auto_mode_settings"]["use_upload_order_fallback"])
        self.assertTrue(project["auto_mode_settings"]["adaptive_pacing"])
        self.assertEqual(len(project["scenes"]), 4)
        self.assertEqual(
            [item["id"] for item in project["media"]],
            [item["id"] for item in uploaded],
        )
        self.assertEqual(
            [scene["match_basis"] for scene in project["scenes"]],
            ["explicit mapping"] * 4,
        )
        export_id = job["export_id"]
        video_path = (
            self.projects / self.project_id / "exports" / export_id / "final.mp4"
        )
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,width,height,r_frame_rate:format=duration",
                "-of", "json", str(video_path),
            ],
            check=True, capture_output=True, text=True,
        )
        info = json.loads(probe.stdout)
        video_stream = next(stream for stream in info["streams"]
                            if stream["codec_type"] == "video")
        self.assertEqual((video_stream["width"], video_stream["height"]), (1920, 1080))
        self.assertEqual(video_stream["r_frame_rate"], "12/1")
        self.assertTrue(any(stream["codec_type"] == "audio"
                            for stream in info["streams"]))
        expected_duration = sum(
            timing["output_duration"] for timing in job["scene_timings"]
        )
        self.assertAlmostEqual(
            float(info["format"]["duration"]), expected_duration, delta=0.15,
        )
        self.assertAlmostEqual(job["duration"], expected_duration, delta=0.15)
        self.assertTrue((video_path.parent / "captions.ass").is_file())
        self.assertEqual(prior_video.read_bytes(), prior_bytes)

        for endpoint in ("video", "download"):
            downloaded = self.client.get(
                f"/api/studio/projects/{self.project_id}/exports/{export_id}/{endpoint}"
            )
            self.assertEqual(downloaded.status_code, 200)
            self.assertEqual(downloaded.headers["content-type"], "video/mp4")
            self.assertIn(b"ftyp", downloaded.content[:16])
        exports = self.client.get("/api/studio/exports").json()
        self.assertIn(export_id, [item["id"] for item in exports])

    def test_metadata_match_order_fallback_missing_and_conflicting_references(self):
        metadata_media = [
            {
                "id": uuid.uuid4().hex,
                "name": "fox.mp4",
                "description": "A red fox crosses a snowy field",
                "keywords": ["red fox", "snow"],
                "characters": ["fox"],
                "location": "winter meadow",
                "mood": "calm",
            },
            {"id": uuid.uuid4().hex, "name": "empty.png"},
        ]
        metadata_plan = build_auto_story_plan(
            parse_master_story_brief("1. A red fox crosses a snowy field."),
            metadata_media, 2.0, False,
        )
        self.assertTrue(metadata_plan["ready"])
        self.assertEqual(metadata_plan["scenes"][0]["media_id"], metadata_media[0]["id"])
        self.assertEqual(metadata_plan["scenes"][0]["match_basis"], "saved metadata match")

        plain_media = [
            {"id": uuid.uuid4().hex, "name": "first.png"},
            {"id": uuid.uuid4().hex, "name": "second.png"},
        ]
        fallback = build_auto_story_plan(
            parse_master_story_brief(
                "1. A quiet beginning.\n2. A distant ending.\n3. A final image."
            ),
            plain_media, 2.0, True,
        )
        self.assertTrue(fallback["ready"])
        self.assertEqual(
            [scene["match_basis"] for scene in fallback["scenes"]],
            ["numbered-order fallback"] * 3,
        )
        self.assertEqual(
            [scene["media_id"] for scene in fallback["scenes"]],
            [plain_media[0]["id"], plain_media[1]["id"], plain_media[0]["id"]],
        )
        self.assertIn("reused", fallback["scenes"][2]["match_details"])

        missing_assets = self.client.post(
            f"/api/studio/projects/{self.project_id}/auto-story/create",
            json=self.create_request("1. A scene. | Asset: 1"),
        )
        self.assertEqual(missing_assets.status_code, 202)
        self.assertEqual(missing_assets.json()["status"], "needs_input")
        self.assertEqual(len(server.STUDIO_RENDER_JOBS), 0)

        for name in ("one.png", "two.png"):
            response = self.client.post(
                f"/api/studio/projects/{self.project_id}/media",
                files={"file": (name, _png_bytes((50, 60, 70)), "image/png")},
            )
            self.assertEqual(response.status_code, 200, response.text)
        ambiguous = self.client.post(
            f"/api/studio/projects/{self.project_id}/auto-story/create",
            json=self.create_request(
                "1. A quiet beginning.\n2. A distant ending.",
                use_upload_order_fallback=False,
            ),
        )
        self.assertEqual(ambiguous.status_code, 202)
        self.assertEqual(ambiguous.json()["status"], "needs_input")
        self.assertEqual(len(server.STUDIO_RENDER_JOBS), 0)
        with patch.object(server, "render_studio_project", return_value={"id": "test-job"}):
            fallback_response = self.client.post(
                f"/api/studio/projects/{self.project_id}/auto-story/create",
                json=self.create_request(
                    "1. A quiet beginning.\n2. A distant ending.",
                    use_upload_order_fallback=True,
                ),
            )
        self.assertEqual(fallback_response.status_code, 202)
        self.assertEqual(fallback_response.json()["status"], "rendering")
        self.assertEqual(
            [row["basis"] for row in fallback_response.json()["plan"]],
            ["numbered-order fallback", "numbered-order fallback"],
        )
        self.assertTrue(
            self.client.get(f"/api/studio/projects/{self.project_id}").json()
            ["auto_mode_settings"]["use_upload_order_fallback"]
        )
        conflict = self.client.post(
            f"/api/studio/projects/{self.project_id}/auto-story/create",
            json=self.create_request("1. A scene. | Asset: 1 | Asset: 2"),
        )
        self.assertEqual(conflict.status_code, 202)
        self.assertEqual(conflict.json()["status"], "needs_input")
        self.assertTrue(any("conflicting asset references" in item.lower()
                            for item in conflict.json()["input_required"]))
        self.assertEqual(len(server.STUDIO_RENDER_JOBS), 0)

    def test_duplicate_job_and_reviewed_manual_assignment_requires_opt_in(self):
        uploaded = self.client.post(
            f"/api/studio/projects/{self.project_id}/media",
            files={"file": ("manual.png", _png_bytes((10, 20, 30)), "image/png")},
        )
        media_id = uploaded.json()["id"]
        manifest_path = self.projects / self.project_id / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        old_scene = {
            "id": uuid.uuid4().hex,
            "text": "A manually reviewed opening.",
            "media_id": media_id,
            "reviewed_media_id": media_id,
            "duration": 1.0,
            "assignment_source": "manual",
            "approved": True,
        }
        manifest["scenes"] = [old_scene]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        changed_brief = "1. A different opening."
        body = self.create_request(changed_brief, use_upload_order_fallback=True)
        conflict = self.client.post(
            f"/api/studio/projects/{self.project_id}/auto-story/create", json=body,
        )
        self.assertEqual(conflict.status_code, 202)
        self.assertEqual(conflict.json()["status"], "needs_input")
        self.assertEqual(conflict.json()["input_required"], [
            "The new brief changes a scene with a reviewed manual media assignment. "
            "Enable “Replace reviewed manual assignments” to proceed."
        ])
        self.assertEqual(
            json.loads(manifest_path.read_text(encoding="utf-8"))["scenes"],
            [old_scene],
        )

        active = server.StudioRenderJob(
            uuid.uuid4().hex, self.project_id, server.StudioRenderReq(),
            server.StudioAudioSettings(),
        )
        active.status = "running"
        server.STUDIO_RENDER_JOBS[active.id] = active
        duplicate = self.client.post(
            f"/api/studio/projects/{self.project_id}/auto-story/create",
            json=self.create_request(changed_brief),
        )
        self.assertEqual(duplicate.status_code, 409)

        server.STUDIO_RENDER_JOBS.clear()
        with patch.object(server, "render_studio_project", return_value={"id": "test-job"}):
            replacement = self.client.post(
                f"/api/studio/projects/{self.project_id}/auto-story/create",
                json=self.create_request(
                    changed_brief, use_upload_order_fallback=True,
                    replace_reviewed_assignments=True,
                ),
            )
        self.assertEqual(replacement.status_code, 202, replacement.text)
        self.assertEqual(replacement.json()["status"], "rendering")
        self.assertEqual(
            replacement.json()["plan"][0]["basis"], "numbered-order fallback",
        )

    def test_numbered_order_reuse_saves_83_scenes_with_23_uploads(self):
        manifest = server._studio_manifest(self.project_id)
        media = []
        project_dir = self.projects / self.project_id
        for index in range(23):
            media_id = uuid.uuid4().hex
            filename = f"{media_id}.png"
            (project_dir / filename).write_bytes(b"uploaded image fixture")
            media.append({
                "id": media_id,
                "name": f"asset-{index + 1}.png",
                "extension": ".png",
                "file": filename,
                "size": len(b"uploaded image fixture"),
            })
        manifest["media"] = media
        server._save_studio_manifest(self.project_id, manifest)

        brief = "\n".join(
            f"{number}. A distinct narrated scene number {number}."
            for number in range(1, 84)
        )
        request = self.create_request(
            brief,
            use_upload_order_fallback=True,
            preview_only=True,
        )
        preview = self.client.post(
            f"/api/studio/projects/{self.project_id}/auto-story/create",
            json=request,
        )
        self.assertEqual(preview.status_code, 202, preview.text)
        self.assertEqual(preview.json()["status"], "preview")
        scenes = preview.json()["scenes"]
        self.assertEqual(len(scenes), 83)
        self.assertEqual(
            [scene["media_id"] for scene in scenes[:23]],
            [item["id"] for item in media],
        )
        self.assertFalse(any(scene["allow_reuse"] for scene in scenes[:23]))
        self.assertTrue(all(scene["allow_reuse"] for scene in scenes[23:]))

        saved = self.client.put(
            f"/api/studio/projects/{self.project_id}/story",
            json={
                "script": "\n".join(scene["text"] for scene in scenes),
                "scenes": scenes,
                "audio_settings": request["audio_settings"],
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(len(saved.json()["scenes"]), 83)
        self.assertEqual(
            [scene["media_id"] for scene in saved.json()["scenes"]],
            [scene["media_id"] for scene in scenes],
        )
        self.assertEqual(
            [scene["text"] for scene in saved.json()["scenes"]],
            [scene["text"] for scene in scenes],
        )
        self.assertTrue(all(
            scene["allow_reuse"] for scene in saved.json()["scenes"][23:]
        ))
        saved_manifest = server._studio_manifest(self.project_id)
        self.assertEqual(saved_manifest["media"], media)
        self.assertEqual(saved.json()["audio_settings"], request["audio_settings"])


if __name__ == "__main__":
    unittest.main()
