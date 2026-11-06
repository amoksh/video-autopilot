import json
import subprocess
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.studio_matching import arrange_scenes, rank_clips_for_scene, suggest_scenes
from webapp import server


def _fixtures():
    clips = [
        {
            "id": uuid.uuid4().hex,
            "name": "rainy-train.mp4",
            "description": "Rainy city train arrives at the platform",
            "keywords": ["train", "platform", "rain"],
            "characters": ["commuter"],
            "location": "metro station",
            "mood": "tense",
        },
        {
            "id": uuid.uuid4().hex,
            "name": "morning-bakery.mp4",
            "description": "Baker kneads fresh bread in a warm bakery",
            "keywords": ["baker", "bread", "flour"],
            "characters": ["baker"],
            "location": "bakery",
            "mood": "warm",
        },
        {
            "id": uuid.uuid4().hex,
            "name": "mountain-hike.mp4",
            "description": "Hiker reaches a mountain summit at sunrise",
            "keywords": ["hiker", "mountain", "sunrise"],
            "characters": ["hiker"],
            "location": "mountain summit",
            "mood": "hopeful",
        },
        {
            "id": uuid.uuid4().hex,
            "name": "cat-window.mp4",
            "description": "A sleepy cat watches birds from a window",
            "keywords": ["cat", "birds", "window"],
            "characters": ["cat"],
            "location": "home",
            "mood": "peaceful",
        },
    ]
    scenes = [
        {
            "id": uuid.uuid4().hex,
            "text": "The commuter waits as a train reaches the platform.",
            "visual_description": "Rain on the city metro station at night",
            "characters": ["commuter"],
            "location": "metro station",
            "mood": "tense",
            "intended_action": "A train arrives",
            "media_id": None,
            "duration": 2.0,
        },
        {
            "id": uuid.uuid4().hex,
            "text": "The baker prepares bread before sunrise.",
            "visual_description": "Hands kneading dough in a warm bakery",
            "characters": ["baker"],
            "location": "bakery",
            "mood": "warm",
            "intended_action": "Knead fresh bread",
            "media_id": None,
            "duration": 2.5,
        },
        {
            "id": uuid.uuid4().hex,
            "text": "A quiet thought changes everything.",
            "visual_description": "",
            "characters": [],
            "location": "",
            "mood": "",
            "intended_action": "",
            "media_id": None,
            "duration": 1.5,
        },
    ]
    media = [
        {
            **clip,
            "extension": ".mp4",
            "size": 1,
            "file": f"{clip['id']}.mp4",
            "technical_metadata": {"duration_seconds": 60},
        }
        for clip in clips
    ]
    return scenes, media


class AutoDirectorTest(unittest.TestCase):
    def test_three_scenes_rank_four_clips_with_uncertainty(self):
        scenes, clips = _fixtures()
        suggestions = suggest_scenes(scenes, clips)

        self.assertEqual(len(suggestions), 3)
        self.assertEqual(suggestions[0]["ranked_clips"][0]["media_id"], clips[0]["id"])
        self.assertEqual(suggestions[1]["ranked_clips"][0]["media_id"], clips[1]["id"])
        self.assertGreater(suggestions[0]["ranked_clips"][0]["score"], 40)
        self.assertEqual(suggestions[0]["ranked_clips"][0]["confidence"], "high")
        self.assertTrue(suggestions[0]["ranked_clips"][0]["reasons"])

        weak = suggestions[2]["ranked_clips"]
        self.assertTrue(all(not clip["matchable"] for clip in weak))
        self.assertTrue(all(clip["confidence"] == "uncertain" for clip in weak))
        missing_metadata = rank_clips_for_scene(
            scenes[0], [{"id": "empty", "name": "unlabeled.mp4"}]
        )[0]
        self.assertEqual(missing_metadata["confidence"], "uncertain")
        arranged = arrange_scenes(scenes, suggestions)
        self.assertIsNone(arranged[2]["media_id"])
        self.assertFalse(any(scene["approved"] for scene in arranged))

    def test_auto_arrange_requires_confirmation_to_replace_manual_assignment(self):
        scenes, media = _fixtures()
        scenes[0]["media_id"] = media[3]["id"]
        scenes[0]["assignment_source"] = "manual"
        scenes[0]["approved"] = True
        project_id = uuid.uuid4().hex

        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            project_dir.mkdir(parents=True)
            for item in media:
                (project_dir / item["file"]).write_bytes(b"fixture")
            (project_dir / "project.json").write_text(json.dumps({
                "id": project_id,
                "name": "Director test",
                "created": "2026-01-01T00:00:00",
                "media": media,
                "scenes": scenes,
            }), encoding="utf-8")
            with patch.object(server, "STUDIO_PROJECTS", projects_dir):
                with self.assertRaises(HTTPException) as required:
                    server.arrange_studio_scenes(
                        project_id, server.StudioArrangeReq()
                    )
                self.assertEqual(required.exception.status_code, 409)
                self.assertEqual(
                    server._studio_manifest(project_id)["scenes"][0]["media_id"],
                    media[3]["id"],
                )

                result = server.arrange_studio_scenes(
                    project_id,
                    server.StudioArrangeReq(confirm_overwrite=True),
                )
                self.assertEqual(result["scenes"][0]["media_id"], media[0]["id"])
                self.assertEqual(result["scenes"][0]["assignment_source"], "auto")
                self.assertFalse(result["scenes"][0]["approved"])
                self.assertIsNone(result["scenes"][2]["media_id"])

    def test_clip_metadata_update_persists_without_touching_source(self):
        project_id = uuid.uuid4().hex
        media_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            project_dir.mkdir(parents=True)
            source = project_dir / f"{media_id}.mp4"
            source.write_bytes(b"untouched source")
            (project_dir / "project.json").write_text(json.dumps({
                "id": project_id,
                "name": "Metadata test",
                "created": "2026-01-01T00:00:00",
                "media": [{
                    "id": media_id, "name": "clip.mp4", "extension": ".mp4",
                    "size": source.stat().st_size, "file": source.name,
                }],
                "exports": [{"id": uuid.uuid4().hex}],
            }), encoding="utf-8")
            with patch.object(server, "STUDIO_PROJECTS", projects_dir):
                result = server.update_studio_media_metadata(
                    project_id,
                    media_id,
                    server.StudioMediaMetadataReq(
                        description="  Foggy bridge at dawn ",
                        keywords=[" bridge ", "", "fog"],
                        characters=[" cyclist "],
                        location=" riverside ",
                        mood=" calm ",
                    ),
                )
                self.assertEqual(result["description"], "Foggy bridge at dawn")
                self.assertEqual(result["keywords"], ["bridge", "fog"])
                self.assertEqual(result["characters"], ["cyclist"])
                self.assertEqual(result["location"], "riverside")
                self.assertEqual(result["mood"], "calm")
                saved = server._studio_manifest(project_id)
                self.assertEqual(len(saved["exports"]), 1)
                self.assertEqual(source.read_bytes(), b"untouched source")
                reloaded = server.studio_project(project_id)
                self.assertEqual(reloaded["media"][0]["description"], "Foggy bridge at dawn")
                self.assertEqual(reloaded["media"][0]["keywords"], ["bridge", "fog"])
                self.assertEqual(reloaded["media"][0]["characters"], ["cyclist"])

    def test_legacy_metadata_strings_are_returned_as_saved_terms(self):
        project_id = uuid.uuid4().hex
        item = {
            "id": uuid.uuid4().hex,
            "name": "legacy.mp4",
            "extension": ".mp4",
            "size": 1,
            "keywords": " train, platform\nrain ",
            "characters": " commuter, driver ",
        }
        with patch.object(server, "STUDIO_PROJECTS", Path("/unused")):
            public = server._studio_media_public(project_id, item)
        self.assertEqual(public["keywords"], ["train", "platform", "rain"])
        self.assertEqual(public["characters"], ["commuter", "driver"])

    def test_technical_metadata_is_extracted_and_dimension_limited(self):
        probe = json.dumps({
            "streams": [{
                "width": 1920, "height": 1080,
                "r_frame_rate": "30000/1001", "duration": "12.5",
            }],
            "format": {"duration": "12.5"},
        })
        completed = subprocess.CompletedProcess([], 0, stdout=probe, stderr="")
        with patch.object(server.subprocess, "run", return_value=completed):
            metadata, error = server._studio_technical_metadata(
                Path("sample.mp4"), ".mp4"
            )
        self.assertIsNone(error)
        self.assertEqual(metadata["duration_seconds"], 12.5)
        self.assertEqual((metadata["width"], metadata["height"]), (1920, 1080))
        self.assertEqual(metadata["frame_rate"], 29.97)

        oversized = json.dumps({
            "streams": [{
                "width": 10000, "height": 1000, "r_frame_rate": "30/1",
            }],
        })
        completed = subprocess.CompletedProcess([], 0, stdout=oversized, stderr="")
        with patch.object(server.subprocess, "run", return_value=completed):
            _, error = server._studio_technical_metadata(Path("large.mp4"), ".mp4")
        self.assertIn("dimensions exceed", error)

    def test_unapproved_or_unassigned_suggestions_cannot_render(self):
        scenes, media = _fixtures()
        project_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            project_dir.mkdir(parents=True)
            for item in media:
                (project_dir / item["file"]).write_bytes(b"fixture")
            scenes[0]["media_id"] = media[0]["id"]
            scenes[0]["approved"] = False
            (project_dir / "project.json").write_text(json.dumps({
                "id": project_id,
                "name": "Approval test",
                "created": "2026-01-01T00:00:00",
                "media": media,
                "scenes": [scenes[0]],
                "story_script": scenes[0]["text"],
            }), encoding="utf-8")
            with patch.object(server, "STUDIO_PROJECTS", projects_dir):
                with self.assertRaises(HTTPException) as approval:
                    server.render_studio_project(
                        project_id, server.StudioRenderReq(fps=12)
                    )
                self.assertEqual(approval.exception.status_code, 400)
                self.assertIn("approve", approval.exception.detail)

    def test_approved_scene_assignments_keep_render_order_and_duration(self):
        scenes, media = _fixtures()
        project_id = uuid.uuid4().hex
        scenes = scenes[:2]
        scenes[0].update(media_id=media[2]["id"], approved=True)
        scenes[1].update(media_id=media[0]["id"], approved=True)
        for scene in scenes:
            scene["assignment_source"] = "auto"
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            project_dir.mkdir(parents=True)
            for item in media:
                (project_dir / item["file"]).write_bytes(b"fixture")
            (project_dir / "project.json").write_text(json.dumps({
                "id": project_id,
                "name": "Render compatibility",
                "created": "2026-01-01T00:00:00",
                "media": media,
                "scenes": scenes,
                "story_script": "\n".join(scene["text"] for scene in scenes),
            }), encoding="utf-8")
            started = threading.Event()

            def capture(*args):
                started.set()

            with (
                patch.object(server, "STUDIO_PROJECTS", projects_dir),
                patch.object(server, "_render_studio_project", side_effect=capture) as worker,
            ):
                job = server.render_studio_project(
                    project_id, server.StudioRenderReq(fps=12)
                )
                self.assertTrue(started.wait(5))
                worker.assert_called_once()
                args = worker.call_args.args
                self.assertEqual(
                    [path.name for path in args[1]],
                    [media[2]["file"], media[0]["file"]],
                )
                self.assertEqual(args[2], [2.0, 2.5])
                self.assertEqual([scene["text"] for scene in args[3]],
                                 [scene["text"] for scene in scenes])
                server.STUDIO_RENDER_JOBS.pop(job["id"], None)

    def test_auto_create_builds_script_assigns_confident_match_and_starts_render(self):
        scenes, media = _fixtures()
        project_id = uuid.uuid4().hex
        script = "Rainy city train arrives at the platform"
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            project_dir.mkdir(parents=True)
            for item in media:
                (project_dir / item["file"]).write_bytes(b"fixture")
            (project_dir / "project.json").write_text(json.dumps({
                "id": project_id,
                "name": "One click",
                "created": "2026-01-01T00:00:00",
                "media": media,
                "story_script": script,
                "scenes": [],
                "audio_settings": {"captions_enabled": True},
            }), encoding="utf-8")
            started = threading.Event()

            def capture(*_args):
                started.set()

            with (
                patch.object(server, "STUDIO_PROJECTS", projects_dir),
                patch.object(server, "_render_studio_project", side_effect=capture) as worker,
            ):
                with TestClient(server.app) as client:
                    response = client.post(
                        f"/api/studio/projects/{project_id}/auto-director/create",
                        json={"fps": 12, "seconds_per_scene": 3},
                    )
                self.assertEqual(response.status_code, 202)
                result = response.json()
                self.assertEqual(result["status"], "rendering")
                self.assertTrue(started.wait(5))
                saved = server._studio_manifest(project_id)
                self.assertEqual(saved["scenes"][0]["text"], script)
                self.assertEqual(saved["scenes"][0]["media_id"], media[0]["id"])
                self.assertTrue(saved["scenes"][0]["approved"])
                self.assertEqual(saved["scenes"][0]["assignment_source"], "auto")
                self.assertTrue(saved["audio_settings"]["captions_enabled"])
                worker.assert_called_once()
                args = worker.call_args.args
                self.assertEqual([path.name for path in args[1]], [media[0]["file"]])
                self.assertEqual(args[2], [3.0])
                server.STUDIO_RENDER_JOBS.pop(result["job"]["id"], None)

    def test_auto_create_preserves_existing_manual_assignment(self):
        scenes, media = _fixtures()
        project_id = uuid.uuid4().hex
        scenes = scenes[:2]
        scenes[0].update(
            media_id=media[3]["id"], assignment_source="manual", approved=True
        )
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            project_dir.mkdir(parents=True)
            for item in media:
                (project_dir / item["file"]).write_bytes(b"fixture")
            (project_dir / "project.json").write_text(json.dumps({
                "id": project_id,
                "name": "Preserve manual",
                "created": "2026-01-01T00:00:00",
                "media": media,
                "scenes": scenes,
                "story_script": "\n".join(scene["text"] for scene in scenes),
            }), encoding="utf-8")
            started = threading.Event()

            with (
                patch.object(server, "STUDIO_PROJECTS", projects_dir),
                patch.object(server, "_render_studio_project",
                             side_effect=lambda *_args: started.set()),
            ):
                result = server.auto_create_studio_video(
                    project_id, server.StudioRenderReq(fps=12)
                )
                self.assertEqual(result["status"], "rendering")
                self.assertTrue(started.wait(5))
                saved = server._studio_manifest(project_id)["scenes"]
                self.assertEqual(saved[0]["media_id"], media[3]["id"])
                self.assertEqual(saved[0]["assignment_source"], "manual")
                self.assertTrue(saved[0]["approved"])
                self.assertEqual(saved[1]["media_id"], media[1]["id"])
                self.assertEqual(saved[1]["assignment_source"], "auto")
                server.STUDIO_RENDER_JOBS.pop(result["job"]["id"], None)

    def test_uncertain_auto_match_can_be_confirmed_and_resume_rendering(self):
        project_id = uuid.uuid4().hex
        media_id = uuid.uuid4().hex
        scene_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            project_dir.mkdir(parents=True)
            media_path = project_dir / f"{media_id}.mp4"
            media_path.write_bytes(b"fixture")
            (project_dir / "project.json").write_text(json.dumps({
                "id": project_id,
                "name": "Needs review",
                "created": "2026-01-01T00:00:00",
                "story_script": "A quiet moment arrives.",
                "scenes": [{
                    "id": scene_id,
                    "text": "A quiet moment arrives.",
                    "media_id": None,
                    "duration": 2.5,
                    "approved": False,
                }],
                "media": [{
                    "id": media_id,
                    "name": "quiet.mp4",
                    "extension": ".mp4",
                    "size": media_path.stat().st_size,
                    "file": media_path.name,
                    "description": "Quiet moment by a window",
                    "keywords": ["quiet", "window"],
                    "technical_metadata": {"duration_seconds": 60},
                }],
            }), encoding="utf-8")
            started = threading.Event()

            def capture(*_args):
                started.set()

            with (
                patch.object(server, "STUDIO_PROJECTS", projects_dir),
                patch.object(server, "_render_studio_project", side_effect=capture) as worker,
            ):
                review = server.auto_create_studio_video(
                    project_id, server.StudioRenderReq(fps=12, seconds_per_scene=2.5)
                )
                self.assertEqual(review["status"], "needs_review")
                self.assertEqual(review["review_scene_ids"], [scene_id])
                self.assertIsNone(server._studio_manifest(project_id)["scenes"][0]["media_id"])
                self.assertTrue(review["suggestions"][0]["ranked_clips"][0]["matchable"])

                server.save_studio_story(
                    project_id,
                    server.StudioStoryReq(
                        script="A quiet moment arrives.",
                        scenes=[server.StudioSceneReq(
                            id=scene_id,
                            text="A quiet moment arrives.",
                            media_id=media_id,
                            reviewed_media_id=media_id,
                            duration=2.5,
                            assignment_source="manual",
                            approved=True,
                        )],
                    ),
                )
                resumed = server.resume_studio_auto_director(
                    project_id,
                    server.StudioAutoDirectorResumeReq(
                        assignments={scene_id: media_id},
                        render_settings=server.StudioRenderReq(
                            fps=12, seconds_per_scene=2.5
                        ),
                    ),
                )
                self.assertEqual(resumed["status"], "rendering")
                self.assertTrue(started.wait(5))
                saved = server._studio_manifest(project_id)["scenes"][0]
                self.assertEqual(saved["media_id"], media_id)
                self.assertTrue(saved["approved"])
                self.assertEqual(saved["assignment_source"], "manual")
                worker.assert_called_once()
                self.assertEqual(worker.call_args.args[2], [2.5])
                server.STUDIO_RENDER_JOBS.pop(resumed["job"]["id"], None)

    def test_two_unmatched_manual_assignments_can_be_reviewed_and_rendered_once(self):
        project_id = uuid.uuid4().hex
        scenes = [{
            "id": uuid.uuid4().hex,
            "text": text,
            "media_id": None,
            "duration": 2.5,
            "approved": False,
            "assignment_source": "manual",
        } for text in ("A superhero stands in a city.", "A robot walks through a forest.")]
        media = []
        for name in ("superhero.mp4", "robot.mp4"):
            media_id = uuid.uuid4().hex
            media.append({
                "id": media_id,
                "name": name,
                "extension": ".mp4",
                "size": 7,
                "file": f"{media_id}.mp4",
                "technical_metadata": {"duration_seconds": 60},
            })

        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            project_dir.mkdir(parents=True)
            for item in media:
                (project_dir / item["file"]).write_bytes(b"fixture")
            (project_dir / "project.json").write_text(json.dumps({
                "id": project_id,
                "name": "Manual overrides",
                "created": "2026-01-01T00:00:00",
                "story_script": "\n".join(scene["text"] for scene in scenes),
                "scenes": scenes,
                "media": media,
            }), encoding="utf-8")
            started = threading.Event()
            with (
                patch.object(server, "STUDIO_PROJECTS", projects_dir),
                patch.object(server, "_render_studio_project",
                             side_effect=lambda *_args: started.set()) as worker,
                TestClient(server.app) as client,
            ):
                created = client.post(
                    f"/api/studio/projects/{project_id}/auto-director/create",
                    json={"fps": 12, "seconds_per_scene": 2.5},
                )
                self.assertEqual(created.status_code, 202)
                review = created.json()
                self.assertEqual(review["status"], "needs_review")
                self.assertEqual(
                    review["review_scene_ids"], [scene["id"] for scene in scenes]
                )
                self.assertTrue(all(
                    not clip["matchable"]
                    for suggestion in review["suggestions"]
                    for clip in suggestion["ranked_clips"]
                ))

                approved_scenes = [{
                    **scene,
                    "media_id": media[index]["id"],
                    "reviewed_media_id": media[index]["id"],
                    "assignment_source": "manual",
                    "approved": True,
                } for index, scene in enumerate(review["scenes"])]
                saved = client.put(
                    f"/api/studio/projects/{project_id}/story",
                    json={
                        "script": "\n".join(scene["text"] for scene in approved_scenes),
                        "scenes": approved_scenes,
                    },
                )
                self.assertEqual(saved.status_code, 200, saved.text)
                assignments = {
                    scene["id"]: scene["media_id"] for scene in approved_scenes
                }
                resume_payload = {
                    "assignments": assignments,
                    "render_settings": {"fps": 12, "seconds_per_scene": 2.5},
                }
                resumed = client.post(
                    f"/api/studio/projects/{project_id}/auto-director/resume",
                    json=resume_payload,
                )
                self.assertEqual(resumed.status_code, 202, resumed.text)
                result = resumed.json()
                self.assertEqual(result["status"], "rendering")
                self.assertEqual(result["job"]["status"], "queued")
                self.assertTrue(started.wait(5))
                saved_scenes = server._studio_manifest(project_id)["scenes"]
                self.assertEqual(
                    [scene["media_id"] for scene in saved_scenes],
                    [scene["media_id"] for scene in approved_scenes],
                )
                self.assertTrue(all(scene["approved"] for scene in saved_scenes))
                worker.assert_called_once()

                duplicate = client.post(
                    f"/api/studio/projects/{project_id}/auto-director/resume",
                    json=resume_payload,
                )
                self.assertEqual(duplicate.status_code, 409)
                worker.assert_called_once()
                server.STUDIO_RENDER_JOBS.pop(result["job"]["id"], None)

    def test_resume_rejects_missing_clip_unchecked_review_and_stale_choice(self):
        project_ids = [uuid.uuid4().hex for _ in range(3)]
        media_ids = [uuid.uuid4().hex, uuid.uuid4().hex]
        scene_ids = [uuid.uuid4().hex for _ in project_ids]
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            for index, project_id in enumerate(project_ids):
                project_dir = projects_dir / project_id
                project_dir.mkdir(parents=True)
                for media_id in media_ids:
                    (project_dir / f"{media_id}.mp4").write_bytes(b"fixture")
                scene = {
                    "id": scene_ids[index],
                    "text": "Scene needs a clip.",
                    "duration": 2.0,
                    "media_id": None if index == 0 else media_ids[0],
                    "approved": index == 2,
                    "reviewed_media_id": media_ids[0] if index == 2 else None,
                    "assignment_source": "manual",
                }
                (project_dir / "project.json").write_text(json.dumps({
                    "id": project_id,
                    "name": f"Negative {index}",
                    "created": "2026-01-01T00:00:00",
                    "story_script": scene["text"],
                    "scenes": [scene],
                    "media": [{
                        "id": media_id,
                        "name": f"clip-{media_id}.mp4",
                        "extension": ".mp4",
                        "size": 7,
                        "file": f"{media_id}.mp4",
                        "technical_metadata": {"duration_seconds": 60},
                    } for media_id in media_ids],
                }), encoding="utf-8")

            with (
                patch.object(server, "STUDIO_PROJECTS", projects_dir),
                TestClient(server.app) as client,
            ):
                missing = client.post(
                    f"/api/studio/projects/{project_ids[0]}/auto-director/resume",
                    json={"assignments": {scene_ids[0]: media_ids[0]}},
                )
                self.assertEqual(missing.status_code, 409)

                unchecked = client.post(
                    f"/api/studio/projects/{project_ids[1]}/auto-director/resume",
                    json={"assignments": {scene_ids[1]: media_ids[0]}},
                )
                self.assertEqual(unchecked.status_code, 409)

                server.save_studio_story(
                    project_ids[2],
                    server.StudioStoryReq(
                        script="Scene needs a clip.",
                        scenes=[server.StudioSceneReq(
                            id=scene_ids[2],
                            text="Scene needs a clip.",
                            media_id=media_ids[1],
                            reviewed_media_id=media_ids[0],
                            duration=2.0,
                            assignment_source="manual",
                            approved=True,
                        )],
                    ),
                )
                stale_saved = server._studio_manifest(project_ids[2])["scenes"][0]
                self.assertFalse(stale_saved["approved"])
                self.assertIsNone(stale_saved["reviewed_media_id"])
                changed = client.post(
                    f"/api/studio/projects/{project_ids[2]}/auto-director/resume",
                    json={"assignments": {scene_ids[2]: media_ids[1]}},
                )
                self.assertEqual(changed.status_code, 409)
                server.save_studio_story(
                    project_ids[2],
                    server.StudioStoryReq(
                        script="Scene needs a clip.",
                        scenes=[server.StudioSceneReq(
                            id=scene_ids[2],
                            text="Scene needs a clip.",
                            media_id=media_ids[1],
                            reviewed_media_id=media_ids[1],
                            duration=2.0,
                            assignment_source="manual",
                            approved=True,
                        )],
                    ),
                )
                started = threading.Event()
                with patch.object(
                    server, "_render_studio_project",
                    side_effect=lambda *_args: started.set(),
                ):
                    freshly_reviewed = client.post(
                        f"/api/studio/projects/{project_ids[2]}/auto-director/resume",
                        json={"assignments": {scene_ids[2]: media_ids[1]}},
                    )
                self.assertEqual(freshly_reviewed.status_code, 202)
                fresh_job_id = freshly_reviewed.json()["job"]["id"]
                self.assertTrue(started.wait(5))
                server.STUDIO_RENDER_JOBS.pop(fresh_job_id, None)

    def test_failed_render_preserves_previous_export(self):
        project_id = uuid.uuid4().hex
        media_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as temporary:
            projects_dir = Path(temporary) / "projects"
            project_dir = projects_dir / project_id
            old_export_id = uuid.uuid4().hex
            old_export = project_dir / "exports" / old_export_id / "final.mp4"
            old_export.parent.mkdir(parents=True)
            old_export.write_bytes(b"previous export")
            media_path = project_dir / f"{media_id}.png"
            media_path.write_bytes(b"source")
            (project_dir / "project.json").write_text(json.dumps({
                "id": project_id,
                "name": "Preserve failed render",
                "created": "2026-01-01T00:00:00",
                "media": [{
                    "id": media_id, "name": "source.png", "extension": ".png",
                    "size": media_path.stat().st_size, "file": media_path.name,
                }],
                "exports": [{"id": old_export_id, "name": "Previous"}],
            }), encoding="utf-8")
            with (
                patch.object(server, "STUDIO_PROJECTS", projects_dir),
                patch.object(server, "assemble_media_only",
                             side_effect=RuntimeError("controlled render failure")),
            ):
                result = server.render_studio_project(
                    project_id, server.StudioRenderReq(fps=12)
                )
                job = server.STUDIO_RENDER_JOBS[result["id"]]
                deadline = threading.Event()
                for _ in range(100):
                    if job.status in ("completed", "failed"):
                        break
                    deadline.wait(0.05)
                self.assertEqual(job.status, "failed")
                self.assertIn("controlled render failure", job.error)
                self.assertEqual(old_export.read_bytes(), b"previous export")
                self.assertEqual(
                    server._studio_manifest(project_id)["exports"],
                    [{"id": old_export_id, "name": "Previous"}],
                )
                self.assertFalse(
                    (project_dir / "exports" / f".staging-{job.id}").exists()
                )
                server.STUDIO_RENDER_JOBS.pop(job.id, None)


if __name__ == "__main__":
    unittest.main()
