"""Web dashboard for Video Autopilot."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Callable, Literal, Optional

import yaml
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

from src.assemble import assemble_media_only
from src.captions import build_scene_captions
from src.output_profiles import OUTPUT_PROFILES, OutputAspectRatio, get_output_profile
from src.studio_matching import (
    arrange_scenes,
    auto_assign_confident_scenes,
    suggest_scenes,
)
from src.studio_auto_story import build_auto_story_plan, parse_master_story_brief
from src.utils import ffprobe_duration, run_ffmpeg
from src.voiceover import synthesize

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
OUTPUT = ROOT / "output"
STUDIO_PROJECTS = OUTPUT / "studio_projects"
STATIC = Path(__file__).resolve().parent / "static"
ALLOWED_MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".mp4"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_STUDIO_PROJECT_BYTES = 2 * 1024 * 1024 * 1024
MAX_STUDIO_TOTAL_SECONDS = 600
MAX_STUDIO_MEDIA_COUNT = 100
MAX_STUDIO_MEDIA_DIMENSION = 8192
MAX_STUDIO_MEDIA_PIXELS = 40_000_000
ADAPTIVE_MIN_SCENE_SECONDS = 2.5
ADAPTIVE_MAX_RECOMMENDED_SECONDS = 6.0

app = FastAPI(title="Video Autopilot")


# ── job manager ──────────────────────────────────────────────────────────────
class Job:
    _ansi = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self, jid: str, label: str, channel: Optional[str] = None):
        self.id = jid
        self.label = label
        self.channel = channel
        self.status = "running"
        self.logs: list[str] = []
        self.started = datetime.now().isoformat(timespec="seconds")
        self.finished: Optional[str] = None

    def line(self, text: str) -> None:
        self.logs.append(self._ansi.sub("", text.rstrip("\n")))

    def as_dict(self, with_logs: bool = False) -> dict:
        d = {"id": self.id, "label": self.label, "channel": self.channel,
             "status": self.status, "started": self.started,
             "finished": self.finished, "log_count": len(self.logs)}
        if with_logs:
            d["logs"] = self.logs
        return d


JOBS: dict[str, Job] = {}


def _run_job(job: Job, args: list[str]) -> None:
    job.line(f"$ python -m src.orchestrator {' '.join(args)}")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "src.orchestrator", *args],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for raw in proc.stdout:  # type: ignore[union-attr]
            job.line(raw)
        proc.wait()
        job.status = "done" if proc.returncode == 0 else "error"
        job.line(f"[exit {proc.returncode}]")
    except Exception as e:
        job.status = "error"
        job.line(f"[fatal] {e}")
    finally:
        job.finished = datetime.now().isoformat(timespec="seconds")


# ── models ───────────────────────────────────────────────────────────────────
class GenerateReq(BaseModel):
    channel: Optional[str] = None
    count: int = 1
    dry_run: bool = True
    all_channels: bool = False


class ConfigReq(BaseModel):
    content: str


class StudioProjectReq(BaseModel):
    name: str


class StudioOrderReq(BaseModel):
    media_ids: list[str]


class StudioMediaMetadataReq(BaseModel):
    description: str = Field(default="", max_length=2000)
    keywords: list[str] = Field(default_factory=list, max_length=50)
    characters: list[str] = Field(default_factory=list, max_length=30)
    location: str = Field(default="", max_length=300)
    mood: str = Field(default="", max_length=300)

    @field_validator("keywords", "characters")
    @classmethod
    def validate_terms(cls, values: list[str]) -> list[str]:
        if any(len(value) > 100 for value in values):
            raise ValueError("Metadata terms must be 100 characters or fewer")
        return values


class StudioShotReq(BaseModel):
    media_id: str
    start: float = Field(ge=0, le=600)
    duration: float = Field(gt=0, le=600)
    source_start: Optional[float] = Field(default=None, ge=0, le=600)
    source_start_auto: Optional[bool] = None


class StudioSceneReq(BaseModel):
    id: str
    text: str
    media_id: Optional[str] = None
    reviewed_media_id: Optional[str] = None
    reviewed_shots: Optional[str] = Field(default=None, max_length=16000)
    duration: float = Field(ge=0.5, le=600)
    shots: list[StudioShotReq] = Field(default_factory=list, max_length=100)
    visual_description: str = Field(default="", max_length=2000)
    characters: list[str] = Field(default_factory=list, max_length=30)
    location: str = Field(default="", max_length=300)
    mood: str = Field(default="", max_length=300)
    intended_action: str = Field(default="", max_length=1000)
    assignment_source: Literal["manual", "auto"] = "manual"
    approved: bool = True
    allow_reuse: bool = False
    scene_number: Optional[int] = None
    match_basis: Optional[Literal[
        "explicit mapping", "saved metadata match", "numbered-order fallback",
        "reviewed manual assignment",
    ]] = None
    match_details: str = Field(default="", max_length=2000)

    @field_validator("characters")
    @classmethod
    def validate_characters(cls, values: list[str]) -> list[str]:
        if any(len(value) > 100 for value in values):
            raise ValueError("Character names must be 100 characters or fewer")
        return values


class StudioArrangeReq(BaseModel):
    confirm_overwrite: bool = False


class StudioAudioSettings(BaseModel):
    voiceover_enabled: bool = False
    captions_enabled: bool = False
    voice: Literal["en-US-AriaNeural", "en-US-GuyNeural"] = "en-US-AriaNeural"
    speech_buffer: float = Field(default=0.5, ge=0.1, le=3)
    caption_size: Literal["small", "medium", "large"] = "medium"
    caption_position: Literal["lower", "middle"] = "lower"


class StudioStoryReq(BaseModel):
    script: str = Field(max_length=50000)
    scenes: list[StudioSceneReq] = Field(default_factory=list, max_length=100)
    audio_settings: StudioAudioSettings = Field(default_factory=StudioAudioSettings)
    master_story_brief: Optional[str] = Field(default=None, max_length=50000)


class StudioAutoStoryInputReq(BaseModel):
    master_story_brief: str = Field(max_length=50000)
    use_upload_order_fallback: bool = False
    adaptive_pacing: bool = False
    audio_settings: StudioAudioSettings = Field(default_factory=StudioAudioSettings)


class StudioQualitySettings(BaseModel):
    aspect_ratio: OutputAspectRatio = "9:16"
    image_motion: bool = True
    transition: Literal["hard_cut", "crossfade"] = "hard_cut"
    export_preset: Literal["legacy", "balanced", "high_quality"] = "legacy"


class StudioRenderReq(StudioQualitySettings):
    fps: int = Field(default=30, ge=12, le=60)
    seconds_per_scene: float = Field(default=2.3, ge=0.5, le=600)
    adaptive_pacing: bool = False


class StudioAutoStoryReq(StudioAutoStoryInputReq):
    render_settings: StudioRenderReq = Field(default_factory=StudioRenderReq)
    replace_reviewed_assignments: bool = False
    preview_only: bool = False


class StudioAutoDirectorResumeReq(BaseModel):
    assignments: dict[str, str] = Field(default_factory=dict, max_length=100)
    render_settings: StudioRenderReq = Field(default_factory=StudioRenderReq)

    @field_validator("assignments")
    @classmethod
    def validate_assignment_ids(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not re.fullmatch(r"[a-f0-9]{32}", scene_id)
               or not re.fullmatch(r"[a-f0-9]{32}", media_id)
               for scene_id, media_id in values.items()):
            raise ValueError("Scene and media IDs must be 32-character hexadecimal values")
        return values


class StudioRenderJob:
    def __init__(self, job_id: str, project_id: str, settings: StudioRenderReq,
                 audio_settings: StudioAudioSettings):
        self.id = job_id
        self.project_id = project_id
        self.settings = settings
        self.audio_settings = audio_settings
        self.status = "queued"
        self.stage = "preparing"
        self.started = datetime.now().isoformat(timespec="seconds")
        self.finished: Optional[str] = None
        self.error: Optional[str] = None
        self.export_id: Optional[str] = None
        self.duration: Optional[float] = None
        self.estimated_duration: Optional[float] = None
        self.size_bytes: Optional[int] = None
        self.video_bitrate: Optional[int] = None
        self.audio_bitrate: Optional[int] = None
        self.scene_timings: list[dict] = []

    def as_dict(self) -> dict:
        profile = get_output_profile(self.settings.aspect_ratio)
        return {
            "id": self.id,
            "project_id": self.project_id,
            "status": self.status,
            "stage": self.stage,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
            "export_id": self.export_id,
            "duration": self.duration,
            "estimated_duration": self.estimated_duration,
            "size_bytes": self.size_bytes,
            "video_bitrate": self.video_bitrate,
            "audio_bitrate": self.audio_bitrate,
            "export_preset": self.settings.export_preset,
            "aspect_ratio": profile.aspect_ratio,
            "width": profile.width,
            "height": profile.height,
            "scene_timings": self.scene_timings,
        }


STUDIO_RENDER_JOBS: dict[str, StudioRenderJob] = {}
STUDIO_RENDER_LOCK = threading.Lock()
STUDIO_MANIFEST_LOCK = threading.RLock()


def _studio_project_dir(project_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", project_id):
        raise HTTPException(404, "project not found")
    directory = (STUDIO_PROJECTS / project_id).resolve()
    if not directory.is_relative_to(STUDIO_PROJECTS.resolve()):
        raise HTTPException(404, "project not found")
    return directory


def _studio_manifest(project_id: str) -> dict:
    directory = _studio_project_dir(project_id)
    manifest_path = directory / "project.json"
    if not manifest_path.is_file():
        raise HTTPException(404, "project not found")
    try:
        with STUDIO_MANIFEST_LOCK:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(500, "project data could not be read") from e
    if not isinstance(manifest, dict) or manifest.get("id") != project_id:
        raise HTTPException(404, "project not found")
    return manifest


def _save_studio_manifest(project_id: str, manifest: dict) -> None:
    directory = _studio_project_dir(project_id)
    temporary = directory / f".project-{uuid.uuid4().hex}.tmp"
    try:
        with STUDIO_MANIFEST_LOCK:
            temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            os.replace(temporary, directory / "project.json")
    finally:
        temporary.unlink(missing_ok=True)


def _update_studio_manifest(project_id: str,
                            update: Callable[[dict], None]) -> dict:
    with STUDIO_MANIFEST_LOCK:
        manifest = _studio_manifest(project_id)
        update(manifest)
        _save_studio_manifest(project_id, manifest)
        return manifest


def _studio_adaptive_pacing(manifest: dict, req: StudioAutoStoryInputReq) -> bool:
    if "adaptive_pacing" in req.model_fields_set:
        return req.adaptive_pacing
    settings = manifest.get("auto_mode_settings", {})
    return bool(settings.get("adaptive_pacing", False)) if isinstance(settings, dict) else False


def _studio_render_settings(
    manifest: dict, settings: StudioRenderReq,
) -> StudioRenderReq:
    saved = StudioQualitySettings.model_validate(manifest.get("quality_settings", {}))
    updates = {}
    if "aspect_ratio" not in settings.model_fields_set:
        updates["aspect_ratio"] = saved.aspect_ratio
    if "export_preset" not in settings.model_fields_set:
        updates["export_preset"] = saved.export_preset
    return settings.model_copy(update=updates) if updates else settings


def _studio_project_summary(manifest: dict) -> dict:
    media = manifest.get("media", [])
    return {
        "id": manifest["id"],
        "name": manifest["name"],
        "created": manifest["created"],
        "media_count": len(media),
    }


def _studio_media_public(project_id: str, item: dict) -> dict:
    media_id = item["id"]
    thumbnail_file = item.get("thumbnail_file")
    return {
        "id": media_id,
        "name": item["name"],
        "extension": item["extension"],
        "size": item["size"],
        "url": f"/api/studio/media/{project_id}/{media_id}",
        "thumbnail_url": (
            f"/api/studio/media/{project_id}/{media_id}/thumbnail"
            if thumbnail_file else (
                f"/api/studio/media/{project_id}/{media_id}"
                if item["extension"] != ".mp4" else None
            )
        ),
        "description": item.get("description", ""),
        "keywords": _studio_metadata_terms(item.get("keywords", [])),
        "characters": _studio_metadata_terms(item.get("characters", [])),
        "location": item.get("location", ""),
        "mood": item.get("mood", ""),
        "technical_metadata": item.get("technical_metadata", {}),
    }


def _studio_metadata_terms(value: object) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else [value]
    return [
        term.strip()
        for item in values
        if isinstance(item, str)
        for term in re.split(r"[,;\n]", item)
        if term.strip()
    ]


def _validated_project_media(project_id: str, manifest: dict) -> list[Path]:
    project_dir = _studio_project_dir(project_id)
    if manifest.get("id") != project_id:
        raise HTTPException(404, "project not found")
    validated = []
    media_items = manifest.get("media", [])
    if not isinstance(media_items, list):
        raise HTTPException(400, "Project media list is invalid")
    if len(media_items) > MAX_STUDIO_MEDIA_COUNT:
        raise HTTPException(
            400, f"A project can contain at most {MAX_STUDIO_MEDIA_COUNT} media files"
        )
    project_bytes = 0
    for item in media_items:
        if not isinstance(item, dict):
            raise HTTPException(400, "Project contains invalid media metadata")
        media_id = item.get("id", "")
        extension = item.get("extension", "")
        expected_file = f"{media_id}{extension}"
        if (not re.fullmatch(r"[a-f0-9]{32}", media_id)
                or extension not in ALLOWED_MEDIA
                or item.get("file") != expected_file):
            raise HTTPException(400, "Project contains invalid media metadata")
        media_path = (project_dir / expected_file).resolve()
        if not media_path.is_relative_to(project_dir) or not media_path.is_file():
            raise HTTPException(400, f"Project media is missing: {item.get('name', 'unnamed file')}")
        size = media_path.stat().st_size
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            raise HTTPException(400, f"Project media has an invalid size: {item.get('name', 'unnamed file')}")
        project_bytes += size
        if project_bytes > MAX_STUDIO_PROJECT_BYTES:
            raise HTTPException(400, "Project media exceeds the 2 GB upload limit")
        validated.append(media_path)
    return validated


def _studio_source_assignments(
    scenes: list[dict], media: list[dict], media_paths: list[Path],
    fps: Optional[int] = None,
) -> list[list[dict]]:
    media_by_id = {
        item["id"]: (item, path) for item, path in zip(media, media_paths)
    }
    used_ranges: dict[str, list[tuple[float, float]]] = {}
    used_images: dict[str, int] = {}
    assignments = []

    def available_duration(item: dict, path: Path) -> float:
        technical = item.get("technical_metadata", {})
        available = (
            technical.get("duration_seconds")
            if isinstance(technical, dict) else None
        )
        if (isinstance(available, bool) or not isinstance(available, (int, float))
                or not math.isfinite(available) or available <= 0):
            try:
                available = ffprobe_duration(path)
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                raise HTTPException(
                    400, f"Could not verify the duration of video "
                    f"{item.get('name', path.name)}: {error}"
                ) from error
        return float(available)

    def aligned_duration(duration: float) -> float:
        return math.ceil(duration * fps - 1e-9) / fps if fps else duration

    for scene_index, scene in enumerate(scenes, 1):
        if not scene.get("shots") and not scene.get("media_id"):
            assignments.append([])
            continue
        raw_shots = scene.get("shots") or [{
            "media_id": scene.get("media_id"),
            "duration": scene.get("duration"),
            "source_start": None,
            "source_start_auto": True,
            "allow_reuse": scene.get("allow_reuse", False),
        }]
        group = []
        for shot_index, shot in enumerate(raw_shots, 1):
            media_id = shot.get("media_id")
            if media_id not in media_by_id:
                raise HTTPException(
                    400, f"Scene {scene_index}, shot {shot_index} references media "
                    "outside this project"
                )
            item, path = media_by_id[media_id]
            duration = shot.get("duration")
            if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                    or not math.isfinite(duration) or duration <= 0):
                raise HTTPException(
                    400, f"Scene {scene_index}, shot {shot_index} has invalid timing"
                )
            duration = aligned_duration(float(duration))
            extension = item.get("extension", path.suffix).lower()
            allow_reuse = bool(scene.get("allow_reuse") or shot.get("allow_reuse"))

            if extension != ".mp4":
                previous_uses = used_images.get(media_id, 0)
                if previous_uses and not allow_reuse:
                    raise HTTPException(
                        400, f"Image {item.get('name', path.name)} is already used "
                        "in this project timeline. Explicitly enable 'Allow reuse' "
                        "for this scene to use it again."
                    )
                used_images[media_id] = previous_uses + 1
                source_start = 0.0
                source_duration = None
            else:
                source_duration = available_duration(item, path)
                requested_start = shot.get("source_start")
                auto_flag = shot.get("source_start_auto")
                auto_start = (
                    auto_flag if isinstance(auto_flag, bool)
                    else requested_start is None
                )
                ranges = used_ranges.setdefault(media_id, [])
                if auto_start or requested_start is None:
                    candidates = [0.0]
                    for range_start, range_end in sorted(ranges):
                        if range_end > candidates[-1]:
                            candidates.append(range_end)
                    source_start = next(
                        (
                            candidate for candidate in candidates
                            if candidate + duration <= source_duration + 0.001
                            and not any(
                                candidate < end - 0.001
                                and candidate + duration > start + 0.001
                                for start, end in ranges
                            )
                        ),
                        None,
                    )
                    if source_start is None:
                        if allow_reuse:
                            source_start = 0.0
                        else:
                            free_intervals = []
                            cursor = 0.0
                            for start, end in sorted(ranges):
                                if start > cursor:
                                    free_intervals.append(start - cursor)
                                cursor = max(cursor, end)
                            if cursor < source_duration:
                                free_intervals.append(source_duration - cursor)
                            free = max(free_intervals, default=0.0)
                            missing = max(0.0, duration - free)
                            raise HTTPException(
                                400, f"Scene {scene_index}, shot {shot_index}: "
                                f"{item.get('name', path.name)} has no unused contiguous "
                                f"source range long enough; {missing:.2f}s of unique "
                                "footage is missing. Assign another uploaded shot, "
                                "shorten the scene/narration, or explicitly enable "
                                "'Allow reuse'."
                            )
                else:
                    if (isinstance(requested_start, bool)
                            or not isinstance(requested_start, (int, float))
                            or not math.isfinite(requested_start)
                            or requested_start < 0):
                        raise HTTPException(
                            400, f"Scene {scene_index}, shot {shot_index} has "
                            "an invalid source start time"
                        )
                    source_start = float(requested_start)
                source_end = source_start + duration
                if source_end > source_duration + 0.001:
                    missing = source_end - source_duration
                    raise HTTPException(
                        400, f"Scene {scene_index}, shot {shot_index}: "
                        f"{item.get('name', path.name)} is {source_duration:.2f}s "
                        f"long but the source range ends at {source_end:.2f}s; "
                        f"{missing:.2f}s of visual coverage is unavailable."
                    )
                overlaps = any(
                    source_start < end - 0.001 and source_end > start + 0.001
                    for start, end in ranges
                )
                if overlaps and not allow_reuse:
                    raise HTTPException(
                        400, f"Scene {scene_index}, shot {shot_index} reuses "
                        f"overlapping footage from {item.get('name', path.name)}. "
                        "Choose an unused source range or explicitly enable "
                        "'Allow reuse'."
                    )
                ranges.append((source_start, source_end))

            group.append({
                "media_id": media_id,
                "source_start": source_start,
                "duration": duration,
                "available_duration": source_duration,
                "allow_reuse": allow_reuse,
                "source_start_auto": (
                    (
                        bool(shot.get("source_start_auto"))
                        if isinstance(shot.get("source_start_auto"), bool)
                        else False
                    )
                    if shot.get("source_start") is not None else True
                ),
            })
        assignments.append(group)
    return assignments


def _studio_record_media_usage(
    project_id: str, scenes: list[dict], media: list[dict],
    fps: Optional[int] = None,
) -> list[dict]:
    media_paths = _validated_project_media(project_id, {
        "id": project_id, "media": media,
    })
    assignments = _studio_source_assignments(scenes, media, media_paths, fps)
    usage = []
    for scene, scene_assignments in zip(scenes, assignments):
        shots = scene.get("shots", [])
        for shot_index, assignment in enumerate(scene_assignments):
            if isinstance(shots, list) and shot_index < len(shots):
                shots[shot_index]["source_start"] = assignment["source_start"]
                shots[shot_index]["source_start_auto"] = assignment["source_start_auto"]
            usage.append({
                "scene_id": scene.get("id"),
                "shot_index": shot_index,
                "media_id": assignment["media_id"],
                "source_start": assignment["source_start"],
                "source_end": (
                    assignment["source_start"] + assignment["duration"]
                    if assignment["available_duration"] is not None
                    else None
                ),
                "allow_reuse": assignment["allow_reuse"],
            })
    return usage


def _studio_scene_shot_groups(
    scenes: list[dict], media: list[dict], media_paths: list[Path], fps: int,
) -> list[list[dict]]:
    media_by_id = {
        item["id"]: (item, path) for item, path in zip(media, media_paths)
    }
    assignments = _studio_source_assignments(scenes, media, media_paths, fps)
    groups = []
    for scene_index, scene in enumerate(scenes, 1):
        scene_duration = scene.get("duration")
        if (isinstance(scene_duration, bool)
                or not isinstance(scene_duration, (int, float))
                or not math.isfinite(scene_duration) or scene_duration <= 0):
            raise HTTPException(400, f"Scene {scene_index} has an invalid duration")
        target_frames = math.ceil(scene_duration * fps - 1e-9)
        raw_shots = scene.get("shots") or [{
            "media_id": scene.get("media_id"),
            "start": 0,
            "duration": scene_duration,
        }]
        if not isinstance(raw_shots, list) or len(raw_shots) > MAX_STUDIO_MEDIA_COUNT:
            raise HTTPException(400, f"Scene {scene_index} has an invalid shot list")

        group = []
        expected_start_frame = 0
        for shot_index, (shot, assignment) in enumerate(
            zip(raw_shots, assignments[scene_index - 1]), 1
        ):
            if not isinstance(shot, dict):
                raise HTTPException(400, f"Scene {scene_index} contains an invalid shot")
            media_id = shot.get("media_id")
            if media_id not in media_by_id:
                raise HTTPException(
                    400, f"Scene {scene_index}, shot {shot_index} references media "
                    "outside this project"
                )
            start = shot.get("start")
            duration = shot.get("duration")
            if (isinstance(start, bool) or not isinstance(start, (int, float))
                    or not math.isfinite(start) or start < 0
                    or isinstance(duration, bool)
                    or not isinstance(duration, (int, float))
                    or not math.isfinite(duration) or duration <= 0):
                raise HTTPException(
                    400, f"Scene {scene_index}, shot {shot_index} has invalid timing"
                )
            start_frame = round(start * fps)
            if (abs(start * fps - start_frame) > 1e-5
                    or start_frame != expected_start_frame):
                raise HTTPException(
                    400, f"Scene {scene_index} shots must be ordered, contiguous, "
                    "and start at 0 seconds"
                )
            shot_frames = math.ceil(duration * fps - 1e-9)
            if shot_frames <= 0:
                raise HTTPException(400, f"Scene {scene_index} has an empty shot")
            aligned_duration = shot_frames / fps
            item, path = media_by_id[media_id]
            available = assignment["available_duration"]
            if available is not None:
                if assignment["source_start"] + aligned_duration > available + 0.001:
                    uncovered = (
                        assignment["source_start"] + aligned_duration - available
                    )
                    raise HTTPException(
                        400,
                        f"Scene {scene_index}, shot {shot_index}: "
                        f"{item.get('name', path.name)} is {available:.2f}s but the "
                        f"shot needs {aligned_duration:.2f}s; {uncovered:.2f}s of "
                        "visual coverage is uncovered. Assign another uploaded shot "
                        "or shorten the scene/narration.",
                    )
            group.append({
                "path": path,
                "media_id": media_id,
                "start": start_frame / fps,
                "source_start": assignment["source_start"],
                "duration": aligned_duration,
                "explicit": bool(scene.get("shots")),
                "available_duration": available,
                "allow_reuse": assignment["allow_reuse"],
            })
            expected_start_frame += shot_frames

        if expected_start_frame != target_frames:
            difference = abs(expected_start_frame - target_frames) / fps
            if expected_start_frame < target_frames:
                detail = f"{difference:.2f}s of scene time is uncovered"
            else:
                detail = f"shots exceed the scene by {difference:.2f}s"
            raise HTTPException(
                400,
                f"Scene {scene_index} shot durations must exactly cover its "
                f"{target_frames / fps:.2f}s narration timeline; {detail}. "
                "Adjust the shot durations or scene duration.",
            )
        groups.append(group)
    return groups


def _studio_shot_review_signature(scene: dict) -> str:
    shots = scene.get("shots")
    if not isinstance(shots, list):
        return "[]"
    try:
        scene_duration = float(scene.get("duration"))
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(scene_duration):
        return ""
    normalized = [f"{scene_duration:.6f}"]
    for shot in shots:
        if not isinstance(shot, dict):
            return ""
        try:
            start = float(shot.get("start"))
            duration = float(shot.get("duration"))
        except (TypeError, ValueError):
            return ""
        if not math.isfinite(start) or not math.isfinite(duration):
            return ""
        normalized.append([
            shot.get("media_id"),
            f"{start:.6f}",
            f"{duration:.6f}",
            (
                None if shot.get("source_start") is None
                else f"{float(shot['source_start']):.6f}"
            ),
            (
                bool(shot.get("source_start_auto"))
                if isinstance(shot.get("source_start_auto"), bool)
                else shot.get("source_start") is None
            ),
        ])
    normalized.insert(1, bool(scene.get("allow_reuse", False)))
    return json.dumps(normalized, separators=(",", ":"))


def _studio_technical_metadata(media_path: Path, extension: str) -> tuple[dict, str | None]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-probesize", "10M",
                "-analyzeduration", "10M", "-threads", "1",
                "-select_streams", "v:0",
                "-show_entries",
                "stream=width,height,r_frame_rate,avg_frame_rate,duration:format=duration",
                "-of", "json", str(media_path),
            ],
            capture_output=True, text=True, check=True, timeout=12,
        )
        probe = json.loads(result.stdout)
        stream = (probe.get("streams") or [{}])[0]
        format_info = probe.get("format") or {}
        width, height = stream.get("width"), stream.get("height")
        if width is not None and height is not None:
            if (width > MAX_STUDIO_MEDIA_DIMENSION
                    or height > MAX_STUDIO_MEDIA_DIMENSION
                    or width * height > MAX_STUDIO_MEDIA_PIXELS):
                raise ValueError(
                    "Media dimensions exceed the Studio limit "
                    "(8192 per side and 40 megapixels)"
                )
        raw_duration = stream.get("duration") or format_info.get("duration")
        try:
            duration = float(raw_duration) if raw_duration is not None else None
        except (TypeError, ValueError):
            duration = None
        if duration is not None and (not math.isfinite(duration) or duration <= 0):
            duration = None
        raw_rate = stream.get("avg_frame_rate")
        if not raw_rate or raw_rate == "0/0":
            raw_rate = stream.get("r_frame_rate")
        frame_rate = None
        if raw_rate and raw_rate != "0/0":
            from fractions import Fraction

            try:
                parsed_rate = float(Fraction(raw_rate))
            except (TypeError, ValueError, ZeroDivisionError):
                parsed_rate = None
            if parsed_rate is not None and math.isfinite(parsed_rate) and parsed_rate > 0:
                frame_rate = round(parsed_rate, 3)
        return {
            "duration_seconds": duration if extension == ".mp4" else None,
            "width": width,
            "height": height,
            "frame_rate": frame_rate,
        }, None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        return {
            "duration_seconds": None,
            "width": None,
            "height": None,
            "frame_rate": None,
        }, (str(error).strip() or error.__class__.__name__)[:300]


def _studio_video_thumbnail(media_path: Path, project_dir: Path,
                            media_id: str, duration: float | None) -> tuple[str | None, str | None]:
    thumbnail = project_dir / f"{media_id}.thumb.jpg"
    timestamp = min(max((duration or 0) * 0.25, 0), 2)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{timestamp:.3f}", "-threads", "1",
                "-i", str(media_path), "-frames:v", "1",
                "-vf", "scale=320:-2",
                "-q:v", "5", str(thumbnail),
            ],
            capture_output=True, text=True, check=True, timeout=15,
        )
        if not thumbnail.is_file() or thumbnail.stat().st_size <= 0:
            thumbnail.unlink(missing_ok=True)
            return None, "FFmpeg produced no thumbnail"
        return thumbnail.name, None
    except (OSError, subprocess.SubprocessError) as error:
        thumbnail.unlink(missing_ok=True)
        return None, (str(error).strip() or error.__class__.__name__)[:300]


def _studio_export_file(project_id: str, export_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", export_id):
        raise HTTPException(404, "export not found")
    manifest = _studio_manifest(project_id)
    if manifest.get("id") != project_id:
        raise HTTPException(404, "project not found")
    exports = manifest.get("exports", [])
    if not isinstance(exports, list):
        raise HTTPException(404, "export not found")
    export = next((entry for entry in exports
                   if isinstance(entry, dict) and entry.get("id") == export_id), None)
    if not export:
        raise HTTPException(404, "export not found")
    project_dir = _studio_project_dir(project_id)
    video_path = (project_dir / "exports" / export_id / "final.mp4").resolve()
    if not video_path.is_relative_to(project_dir) or not video_path.is_file():
        raise HTTPException(404, "export not found")
    return video_path


def _validate_scene_timeline(timings: list[dict], expected_duration: float,
                             fps: int) -> None:
    offset = 0.0
    tolerance = 1 / fps + 0.005
    for index, timing in enumerate(timings, 1):
        start = timing.get("start")
        end = timing.get("end")
        caption_end = timing.get("caption_end")
        output_duration = timing.get("output_duration")
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in (start, end, caption_end, output_duration)
        ):
            raise ValueError(f"Scene {index} has invalid render or caption timestamps")
        if (abs(start - offset) > tolerance
                or end <= start
                or abs((end - start) - output_duration) > tolerance
                or caption_end < start
                or caption_end > end + 0.01):
            raise ValueError(
                f"Scene {index} caption/audio timing falls outside its rendered scene"
            )
        offset = end
    if timings and abs(offset - expected_duration) > tolerance:
        raise ValueError(
            "Scene timeline duration does not match the frame-aligned video duration"
        )


def _studio_export_metrics(video_path: Path, expected_duration: float,
                           expected_audio: bool, fps: int,
                           last_caption_end: Optional[float],
                           aspect_ratio: str = "9:16") -> dict:
    profile = get_output_profile(aspect_ratio)
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration,size,bit_rate:"
            "stream=codec_type,codec_name,width,height,pix_fmt,avg_frame_rate,"
            "duration,bit_rate,nb_frames",
            "-of", "json", str(video_path),
        ],
        capture_output=True, text=True, check=True, timeout=30,
    )
    info = json.loads(probe.stdout)
    streams = info.get("streams", [])
    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )
    if not video or video.get("codec_name") != "h264":
        raise RuntimeError("Export is missing its browser-compatible H.264 video stream")
    if (video.get("width") != profile.width
            or video.get("height") != profile.height
            or video.get("pix_fmt") != "yuv420p"):
        raise RuntimeError(
            f"Export must use {profile.width}x{profile.height} yuv420p video "
            "for browser-compatible playback"
        )
    try:
        frame_rate = float(Fraction(video.get("avg_frame_rate", "")))
    except (TypeError, ValueError, ZeroDivisionError) as error:
        raise RuntimeError("Export has an invalid video frame rate") from error
    if abs(frame_rate - fps) > 0.01:
        raise RuntimeError(
            f"Export frame rate does not match the requested {fps} FPS "
            f"(got {frame_rate:g} FPS)"
        )
    if expected_audio and (not audio or audio.get("codec_name") != "aac"):
        raise RuntimeError("Export is missing its expected browser-compatible AAC audio")
    if not expected_audio and audio:
        raise RuntimeError("Silent project export unexpectedly contains an audio stream")

    video_duration = float(video.get("duration") or info["format"]["duration"])
    duration_tolerance = 1 / fps + 0.04
    if abs(video_duration - expected_duration) > duration_tolerance:
        raise RuntimeError(
            "Exported video stream duration does not match the scene timeline "
            f"(expected {expected_duration:.3f}s, got {video_duration:.3f}s)"
        )
    audio_duration = float(audio["duration"]) if audio and audio.get("duration") else None
    if audio_duration is not None:
        if last_caption_end is not None and audio_duration + duration_tolerance < last_caption_end:
            raise RuntimeError(
                "Encoded narration ends before the final scene caption timestamp"
            )
        if audio_duration > expected_duration + duration_tolerance:
            raise RuntimeError("Encoded narration exceeds the scene timeline")

    return {
        "size_bytes": video_path.stat().st_size,
        "video_bitrate": int(video["bit_rate"]) if video.get("bit_rate") else None,
        "audio_bitrate": (
            int(audio["bit_rate"]) if audio and audio.get("bit_rate") else None
        ),
        "video_duration": video_duration,
        "audio_duration": audio_duration,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "frame_rate": video.get("avg_frame_rate"),
    }


def _render_studio_project(job: StudioRenderJob, clips: list[Path],
                           scene_durations: Optional[list[float]] = None,
                           scenes: Optional[list[dict]] = None,
                           scene_visuals: Optional[list[list[dict]]] = None) -> None:
    project_dir = _studio_project_dir(job.project_id)
    profile = get_output_profile(job.settings.aspect_ratio)
    exports_dir = project_dir / "exports"
    staging_dir = exports_dir / f".staging-{job.id}"
    final_dir = exports_dir / job.id
    try:
        job.status = "running"
        exports_dir.mkdir(parents=True, exist_ok=True)
        staging_dir.mkdir()
        audio_path = None
        captions_path = None
        effective_durations = scene_durations
        estimate_durations = (
            effective_durations
            if effective_durations is not None
            else [job.settings.seconds_per_scene] * len(clips)
        )
        aligned_estimates = [
            math.ceil(duration * job.settings.fps - 1e-9) / job.settings.fps
            for duration in estimate_durations
        ]
        job.estimated_duration = sum(aligned_estimates)
        if scenes:
            if effective_durations is None or len(effective_durations) != len(scenes):
                raise ValueError("Story scene timing data is invalid")
            timings = []
            offset = 0.0
            requested_durations = effective_durations
            effective_durations = [
                math.ceil(duration * job.settings.fps - 1e-9) / job.settings.fps
                for duration in requested_durations
            ]
            for index, (scene, requested, effective) in enumerate(
                zip(scenes, requested_durations, effective_durations), 1
            ):
                timings.append({
                    "scene": index,
                    "start": offset,
                    "end": offset + effective,
                    "caption_end": offset + effective,
                    "requested_duration": requested,
                    "narration_duration": None,
                    "output_duration": effective,
                    "extended": False,
                })
                offset += effective

            if job.audio_settings.voiceover_enabled:
                job.stage = "synthesizing_voiceover"
                audio_path, effective_durations, timings = _studio_voiceover(
                    scenes, requested_durations, job.audio_settings, staging_dir,
                    job.settings.fps, job.settings.adaptive_pacing,
                )
                job.estimated_duration = sum(effective_durations)
            _validate_scene_timeline(
                timings, sum(effective_durations), job.settings.fps,
            )
            job.scene_timings = timings
            if job.audio_settings.captions_enabled:
                captions_path = build_scene_captions(
                    scenes, timings, staging_dir / "captions.ass",
                    profile.width, profile.height,
                    job.audio_settings.caption_size,
                    job.audio_settings.caption_position,
                )

        job.stage = "rendering"
        render_clips = clips
        render_durations = effective_durations
        render_source_starts = None
        if scene_visuals is not None and effective_durations is not None:
            if len(scene_visuals) != len(effective_durations):
                raise ValueError("Story shot groups do not match the narration scenes")
            render_clips = []
            render_durations = []
            for scene_index, (group, scene_duration) in enumerate(
                zip(scene_visuals, effective_durations), 1
            ):
                target_frames = round(scene_duration * job.settings.fps)
                shots = [dict(shot) for shot in group]
                if len(shots) == 1 and not shots[0]["explicit"]:
                    shots[0]["start"] = 0
                    shots[0]["duration"] = scene_duration

                current_frame = 0
                for shot_index, shot in enumerate(shots, 1):
                    duration_frames = math.ceil(
                        shot["duration"] * job.settings.fps - 1e-9
                    )
                    if (shot_index == len(shots)
                            and current_frame + duration_frames < target_frames
                            and shot["path"].suffix.lower()
                            in (".png", ".jpg", ".jpeg", ".webp")):
                        duration_frames = target_frames - current_frame
                    start_frame = round(shot["start"] * job.settings.fps)
                    if start_frame != current_frame:
                        raise ValueError(
                            f"Scene {scene_index} shot {shot_index} is not "
                            "contiguous with the previous shot"
                        )
                    if shot["available_duration"] is not None:
                        aligned_duration = duration_frames / job.settings.fps
                        source_end = shot["source_start"] + aligned_duration
                        if source_end > shot["available_duration"] + 0.001:
                            uncovered = source_end - shot["available_duration"]
                            raise ValueError(
                                f"Scene {scene_index}, shot {shot_index} has "
                                f"{uncovered:.2f}s of unique footage missing for its "
                                "narration timing. Add an unused shot, shorten the "
                                "scene/narration, or explicitly approve footage reuse."
                            )
                    render_clips.append(shot["path"])
                    render_durations.append(duration_frames / job.settings.fps)
                    if render_source_starts is None:
                        render_source_starts = []
                    render_source_starts.append(shot["source_start"])
                    current_frame += duration_frames
                if current_frame != target_frames:
                    uncovered = abs(current_frame - target_frames) / job.settings.fps
                    detail = (
                        f"{uncovered:.2f}s is uncovered"
                        if current_frame < target_frames
                        else f"shots exceed the scene by {uncovered:.2f}s"
                    )
                    raise ValueError(
                        f"Scene {scene_index} shot timeline does not match its "
                        f"{scene_duration:.2f}s narration duration; {detail}. "
                        "Adjust shot durations or add an explicitly positioned shot."
                    )
        output_path, duration = assemble_media_only(
            render_clips, staging_dir, job.settings.fps, job.settings.seconds_per_scene,
            render_durations, audio_path, captions_path,
            job.settings.image_motion, job.settings.transition,
            job.settings.export_preset,
            aspect_ratio=profile.aspect_ratio,
            source_starts=render_source_starts,
        )
        metrics = _studio_export_metrics(
            output_path, duration, audio_path is not None, job.settings.fps,
            job.scene_timings[-1]["caption_end"] if job.scene_timings else None,
            profile.aspect_ratio,
        )
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_dir, final_dir)

        export = {
            "id": job.id,
            "created": datetime.now().isoformat(timespec="seconds"),
            "duration": duration,
            "fps": job.settings.fps,
            **metrics,
            "export_preset": job.settings.export_preset,
            "scene_timings": job.scene_timings,
        }
        def add_export(manifest: dict) -> None:
            export["name"] = f"{manifest['name']} export"
            manifest.setdefault("exports", []).append(export)
        _update_studio_manifest(job.project_id, add_export)
        job.duration = duration
        job.size_bytes = metrics["size_bytes"]
        job.video_bitrate = metrics["video_bitrate"]
        job.audio_bitrate = metrics["audio_bitrate"]
        job.export_id = job.id
        job.status = "completed"
        job.stage = "completed"
    except Exception as error:
        message = str(error).replace(str(ROOT), "[workspace]")
        job.error = message[-2000:] or error.__class__.__name__
        job.status = "failed"
        job.stage = "failed"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
    finally:
        job.finished = datetime.now().isoformat(timespec="seconds")


def _studio_voiceover(scenes: list[dict], requested_durations: list[float],
                      audio_settings: StudioAudioSettings,
                      staging_dir: Path, fps: int,
                      adaptive_pacing: bool = False) -> tuple[Path, list[float], list[dict]]:
    cfg = _read_yaml("settings.yaml")
    cfg.setdefault("tts", {})
    cfg["tts"]["provider"] = "edge"
    cfg["tts"]["edge_voice"] = audio_settings.voice
    cfg["tts"]["speed"] = 1.0
    cfg["tts"]["edge_rate"] = "+0%"
    cfg["tts"]["edge_pitch"] = "+0Hz"
    cfg["tts"]["studio_safe_mastering"] = True

    voice_files = []
    durations = []
    timings = []
    offset = 0.0
    for index, (scene, requested) in enumerate(zip(scenes, requested_durations), 1):
        text = scene.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Scene {index} needs narration text when voiceover is enabled")
        voice_dir = staging_dir / "narration" / f"scene_{index:03d}"
        voice_dir.mkdir(parents=True, exist_ok=False)
        try:
            voice_file = synthesize(cfg, text.strip(), voice_dir)
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            raise RuntimeError(
                f"Edge TTS failed for scene {index} ({audio_settings.voice}): {detail}"
            ) from error
        if not voice_file.is_file() or voice_file.stat().st_size == 0:
            raise RuntimeError(f"Edge TTS returned no audio for scene {index}")
        try:
            spoken = ffprobe_duration(voice_file)
        except Exception as error:
            raise RuntimeError(f"Could not read generated narration for scene {index}") from error
        if not math.isfinite(spoken) or spoken <= 0:
            raise RuntimeError(f"Edge TTS returned an invalid duration for scene {index}")

        spoken_with_tail = spoken + audio_settings.speech_buffer
        pacing_target = min(
            max(spoken_with_tail, ADAPTIVE_MIN_SCENE_SECONDS),
            ADAPTIVE_MAX_RECOMMENDED_SECONDS,
        ) if adaptive_pacing else 0
        fitted_duration = max(requested, spoken_with_tail, pacing_target)
        output_duration = math.ceil(fitted_duration * fps - 1e-9) / fps
        if output_duration > MAX_STUDIO_TOTAL_SECONDS:
            raise ValueError(
                f"Scene {index} narration exceeds the {MAX_STUDIO_TOTAL_SECONDS}-second render limit"
            )
        timings.append({
            "scene": index,
            "start": offset,
            "end": offset + output_duration,
            "caption_end": offset + spoken,
            "requested_duration": requested,
            "narration_duration": spoken,
            "adaptive_recommended_duration": pacing_target or None,
            "output_duration": output_duration,
            "extended": output_duration > requested,
        })
        offset += output_duration
        durations.append(output_duration)
        voice_files.append(voice_file)

    if offset > MAX_STUDIO_TOTAL_SECONDS:
        raise ValueError(
            f"Total render duration exceeds the {MAX_STUDIO_TOTAL_SECONDS}-second limit"
        )

    filters = []
    labels = []
    args = []
    for index, (voice_file, timing) in enumerate(zip(voice_files, timings)):
        args.extend(["-i", str(voice_file)])
        label = f"scene_audio_{index}"
        padding = timing["output_duration"] - timing["narration_duration"]
        filters.append(
            f"[{index}:a:0]aresample=48000,aformat=channel_layouts=stereo,"
            f"apad=pad_dur={padding:.6f},"
            f"atrim=duration={timing['output_duration']:.6f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")
    filters.append(
        f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[joined];"
        "[joined]loudnorm=I=-18:TP=-2:LRA=11,"
        f"alimiter=limit=0.8:attack=5:release=50:level=false,"
        f"apad=pad_dur={offset:.9f},atrim=duration={offset:.9f}[outa]"
    )
    audio_path = staging_dir / "narration.m4a"
    run_ffmpeg([
        *args, "-filter_complex", ";".join(filters), "-map", "[outa]",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-t", f"{offset:.9f}", str(audio_path),
    ])
    encoded_audio_duration = ffprobe_duration(audio_path)
    if abs(encoded_audio_duration - offset) > max(0.15, 1 / fps + 0.04):
        raise RuntimeError(
            "Concatenated narration duration does not match the padded scene timeline "
            f"(expected {offset:.3f}s, got {encoded_audio_duration:.3f}s)"
        )
    offset = 0.0
    for timing in timings:
        timing["start"] = offset
        timing["end"] = offset + timing["output_duration"]
        timing["caption_end"] = offset + timing["narration_duration"]
        offset = timing["end"]
    return audio_path, durations, timings


# ── config ───────────────────────────────────────────────────────────────────
def _read_yaml(name: str) -> dict:
    return yaml.safe_load((CONFIG / name).read_text(encoding="utf-8")) or {}


@app.get("/api/channels")
def channels():
    data = _read_yaml("channels.yaml").get("channels", {})
    hist = {}
    hf = OUTPUT / "history.json"
    if hf.exists():
        hist = json.loads(hf.read_text(encoding="utf-8"))
    out = []
    for key, c in data.items():
        topics = hist.get(key, {}).get("topics", [])
        out.append({
            "key": key, "name": c.get("name", key), "niche": c.get("niche"),
            "tone": c.get("tone"), "audience": c.get("audience", ""),
            "angle": c.get("angle", ""), "voice": c.get("voice", ""),
            "visual_style": c.get("visual_style", ""),
            "music_mood": c.get("music_mood", ""),
            "platforms": c.get("platforms", []),
            "posts_per_day": c.get("posts_per_day", 1),
            "produced": len(topics),
            "recent_topics": topics[-5:],
        })
    return out


@app.get("/api/config/{name}")
def get_config(name: str):
    if name not in ("settings.yaml", "channels.yaml"):
        raise HTTPException(404, "unknown config file")
    return {"name": name, "content": (CONFIG / name).read_text(encoding="utf-8")}


@app.put("/api/config/{name}")
def put_config(name: str, req: ConfigReq):
    if name not in ("settings.yaml", "channels.yaml"):
        raise HTTPException(404, "unknown config file")
    try:
        yaml.safe_load(req.content)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"Invalid YAML: {e}")
    (CONFIG / name).write_text(req.content, encoding="utf-8")
    return {"ok": True}


def _key_present(v) -> bool:
    """A key counts as set only if it's non-empty and not a leftover comment/placeholder."""
    if not v:
        return False
    v = v.strip()
    return bool(v) and not v.startswith("#")


@app.get("/api/env-status")
def env_status():
    from dotenv import dotenv_values
    vals = dotenv_values(ROOT / ".env")
    keys = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY",
            "OPENROUTER_API_KEY", "GEMINI_API_KEY", "ELEVENLABS_API_KEY",
            "PEXELS_API_KEY", "FAL_KEY", "REPLICATE_API_TOKEN",
            "AYRSHARE_API_KEY", "UPLOAD_POST_API_KEY"]
    return {k: _key_present(vals.get(k)) for k in keys}


# ── generate ─────────────────────────────────────────────────────────────────
@app.post("/api/generate")
def generate(req: GenerateReq):
    args: list[str] = []
    channel = None
    if req.all_channels:
        args.append("--all")
        label = "All channels"
    elif req.channel:
        args += ["--channel", req.channel, "--count", str(req.count)]
        label = f"{req.channel} ×{req.count}"
        channel = req.channel
    else:
        raise HTTPException(400, "provide a channel or set all_channels")
    if req.dry_run:
        args.append("--dry-run")
        label += " · dry-run"

    jid = uuid.uuid4().hex[:8]
    job = Job(jid, label, channel)
    JOBS[jid] = job
    threading.Thread(target=_run_job, args=(job, args), daemon=True).start()
    return {"job_id": jid}


@app.get("/api/jobs")
def list_jobs():
    return [j.as_dict() for j in sorted(JOBS.values(), key=lambda x: x.started, reverse=True)]


@app.get("/api/jobs/{jid}")
def job_detail(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "no such job")
    return job.as_dict(with_logs=True)


# ── library ──────────────────────────────────────────────────────────────────
@app.get("/api/library")
def library():
    items = []
    for meta_file in sorted(OUTPUT.glob("*/*/*/metadata.json"), reverse=True):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        d = meta_file.parent
        rel = d.relative_to(OUTPUT).as_posix()
        parts = rel.split("/")
        script_preview = ""
        sf = d / "script.txt"
        if sf.exists():
            script_preview = sf.read_text(encoding="utf-8")[:200]
        items.append({
            "rel": rel,
            "date": parts[0] if len(parts) > 0 else "",
            "channel": parts[1] if len(parts) > 1 else "",
            "slug": parts[2] if len(parts) > 2 else "",
            "title": (meta.get("metadata", {}).get("title")
                      or meta.get("idea", {}).get("title", rel)),
            "description": meta.get("metadata", {}).get("description", ""),
            "hashtags": meta.get("metadata", {}).get("hashtags", []),
            "status": meta.get("publish", {}).get("status", "?"),
            "has_video": (d / "final.mp4").exists(),
            "has_thumb": (d / "thumb.jpg").exists(),
            "script_preview": script_preview,
            "niche": meta.get("channel", {}).get("niche", ""),
        })
    return items[:200]


@app.get("/api/library/{rel:path}/meta")
def library_meta(rel: str):
    f = (OUTPUT / rel / "metadata.json").resolve()
    if not str(f).startswith(str(OUTPUT.resolve())) or not f.exists():
        raise HTTPException(404, "not found")
    return JSONResponse(json.loads(f.read_text(encoding="utf-8")))


@app.delete("/api/library/{rel:path}")
def library_delete(rel: str):
    d = (OUTPUT / rel).resolve()
    if not str(d).startswith(str(OUTPUT.resolve())) or not d.exists():
        raise HTTPException(404, "not found")
    shutil.rmtree(d)
    return {"ok": True}


@app.get("/api/video/{rel:path}")
def video(rel: str):
    f = (OUTPUT / rel / "final.mp4").resolve()
    if not str(f).startswith(str(OUTPUT.resolve())) or not f.exists():
        raise HTTPException(404, "not found")
    return FileResponse(f, media_type="video/mp4")


@app.get("/api/thumbnail/{rel:path}")
def thumbnail(rel: str):
    d = (OUTPUT / rel).resolve()
    if not str(d).startswith(str(OUTPUT.resolve())):
        raise HTTPException(404)
    video_path = d / "final.mp4"
    thumb = d / "thumb.jpg"
    if not video_path.exists():
        raise HTTPException(404, "no video")
    if not thumb.exists():
        proc = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(video_path), "-ss", "1", "-vframes", "1",
             "-vf", "scale=360:-1", str(thumb)],
            capture_output=True,
        )
        if proc.returncode != 0 or not thumb.exists():
            raise HTTPException(500, "thumb extraction failed")
    return FileResponse(thumb, media_type="image/jpeg")


# ── studio projects ──────────────────────────────────────────────────────────
@app.get("/api/studio/projects")
def studio_projects():
    projects = []
    if not STUDIO_PROJECTS.exists():
        return projects
    for manifest_path in STUDIO_PROJECTS.glob("*/project.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            projects.append(_studio_project_summary(manifest))
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            continue
    return sorted(projects, key=lambda project: project["created"], reverse=True)


@app.post("/api/studio/projects")
def create_studio_project(req: StudioProjectReq):
    name = " ".join(req.name.split())
    if not name or len(name) > 80:
        raise HTTPException(400, "project name must be 1–80 characters")
    STUDIO_PROJECTS.mkdir(parents=True, exist_ok=True)
    project_id = uuid.uuid4().hex
    directory = _studio_project_dir(project_id)
    directory.mkdir()
    manifest = {
        "id": project_id,
        "name": name,
        "created": datetime.now().isoformat(timespec="seconds"),
        "media": [],
        "quality_settings": StudioQualitySettings(
            export_preset="balanced",
        ).model_dump(),
        "auto_mode_settings": {"adaptive_pacing": False},
    }
    _save_studio_manifest(project_id, manifest)
    return _studio_project_summary(manifest)


@app.get("/api/studio/projects/{project_id}")
def studio_project(project_id: str):
    manifest = _studio_manifest(project_id)
    if manifest.get("id") != project_id:
        raise HTTPException(404, "project not found")
    scenes = manifest.get("scenes", [])
    if not isinstance(scenes, list) or any(
        not isinstance(scene, dict) for scene in scenes
    ):
        raise HTTPException(400, "Project story data is invalid")
    video_settings = _read_yaml("settings.yaml").get("video", {})
    quality_settings = StudioQualitySettings.model_validate(
        manifest.get("quality_settings", {})
    )
    profile = get_output_profile(quality_settings.aspect_ratio)
    return {
        **_studio_project_summary(manifest),
        "output_profiles": {
            key: {
                "aspect_ratio": profile.aspect_ratio,
                "width": profile.width,
                "height": profile.height,
                "label": profile.label,
            }
            for key, profile in OUTPUT_PROFILES.items()
        },
        "aspect_ratio": profile.aspect_ratio,
        "width": profile.width,
        "height": profile.height,
        "render_defaults": {
            "fps": video_settings.get("fps", 30),
            "seconds_per_scene": video_settings.get("cut_every_seconds", 2.3),
            "aspect_ratio": profile.aspect_ratio,
            "export_preset": quality_settings.export_preset,
        },
        "quality_settings": quality_settings.model_dump(),
        "media": [
            _studio_media_public(project_id, item)
            for item in manifest.get("media", [])
        ],
        "story_script": manifest.get("story_script", ""),
        "master_story_brief": manifest.get("master_story_brief", ""),
        "auto_mode_settings": manifest.get("auto_mode_settings", {}),
        "scenes": [
            {
                **scene,
                "characters": _studio_metadata_terms(scene.get("characters", [])),
            }
            for scene in scenes
        ],
        "audio_settings": manifest.get("audio_settings", {}),
    }


@app.put("/api/studio/projects/{project_id}/quality-settings")
def save_studio_quality_settings(project_id: str, req: StudioQualitySettings):
    def save_quality(manifest: dict) -> None:
        current = StudioQualitySettings.model_validate(
            manifest.get("quality_settings", {})
        )
        manifest["quality_settings"] = req.model_dump()
        if "export_preset" not in req.model_fields_set:
            manifest["quality_settings"]["export_preset"] = current.export_preset
        if "aspect_ratio" not in req.model_fields_set:
            manifest["quality_settings"]["aspect_ratio"] = current.aspect_ratio

    manifest = _update_studio_manifest(project_id, save_quality)
    return {"quality_settings": manifest["quality_settings"]}


def _studio_scene_blocks(script: str, duration: float) -> list[dict]:
    lines = [line.strip() for line in script.splitlines() if line.strip()]
    if len(lines) > 100:
        raise HTTPException(400, "A story can contain at most 100 scenes")
    return [
        {
            "id": uuid.uuid4().hex,
            "text": text,
            "media_id": None,
            "reviewed_media_id": None,
            "duration": duration,
            "visual_description": "",
            "characters": [],
            "location": "",
            "mood": "",
            "intended_action": "",
            "assignment_source": "manual",
            "approved": False,
        }
        for text in lines
    ]


@app.put("/api/studio/projects/{project_id}/story")
def save_studio_story(project_id: str, req: StudioStoryReq):
    def save_story(manifest: dict) -> None:
        media = manifest.get("media", [])
        if not isinstance(media, list):
            raise HTTPException(400, "Project media list is invalid")
        media_ids = {
            item.get("id") for item in media
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        scene_ids = set()
        existing_scenes = {
            scene.get("id"): scene for scene in manifest.get("scenes", [])
            if isinstance(scene, dict)
        }
        scenes = []
        for scene in req.scenes:
            if not re.fullmatch(r"[a-f0-9]{32}", scene.id) or scene.id in scene_ids:
                raise HTTPException(400, "Scene IDs must be unique 32-character hexadecimal values")
            if not scene.text.strip():
                raise HTTPException(400, "Scene text cannot be empty")
            if len(scene.text) > 5000:
                raise HTTPException(400, "Scene text must be 5000 characters or fewer")
            referenced_media_ids = (
                [shot.media_id for shot in scene.shots]
                if scene.shots else ([scene.media_id] if scene.media_id else [])
            )
            if any(media_id not in media_ids for media_id in referenced_media_ids):
                raise HTTPException(400, "A scene references media outside this project")
            scene_ids.add(scene.id)
            saved_scene = scene.model_dump()
            if scene.shots:
                saved_scene["media_id"] = scene.shots[0].media_id
            saved_scene["_incoming_review_signature"] = (
                _studio_shot_review_signature(saved_scene)
            )
            selected_media_id = saved_scene["media_id"]
            scenes.append(saved_scene)

        used_media = any(
            scene.get("shots") or scene.get("media_id") for scene in scenes
        )
        usage = []
        if used_media:
            media_paths = _validated_project_media(project_id, manifest)
            assignments = _studio_source_assignments(scenes, media, media_paths)
            for scene, scene_assignments in zip(scenes, assignments):
                requested_signature = scene.pop("_incoming_review_signature")
                if scene.get("shots"):
                    for shot, assignment in zip(scene["shots"], scene_assignments):
                        shot["source_start"] = assignment["source_start"]
                        shot["source_start_auto"] = assignment["source_start_auto"]
                    shot_signature = _studio_shot_review_signature(scene)
                    reviewed = (
                        scene.get("approved") is True
                        and scene.get("reviewed_shots") == requested_signature
                    )
                    scene["approved"] = reviewed
                    scene["reviewed_media_id"] = (
                        scene.get("media_id") if reviewed else None
                    )
                    scene["reviewed_shots"] = shot_signature if reviewed else None
                else:
                    selected_media_id = scene.get("media_id")
                    previous = existing_scenes.get(scene.get("id"))
                    if scene.get("approved") is True and selected_media_id:
                        if scene.get("reviewed_media_id") == selected_media_id:
                            scene["reviewed_media_id"] = selected_media_id
                        elif scene.get("assignment_source") == "auto" and scene.get("match_basis"):
                            scene["approved"] = False
                            scene["reviewed_media_id"] = None
                        elif previous and previous.get("media_id") != selected_media_id:
                            scene["approved"] = False
                            scene["reviewed_media_id"] = None
                        else:
                            scene["reviewed_media_id"] = selected_media_id
                    else:
                        scene["approved"] = False
                        scene["reviewed_media_id"] = None
                for shot_index, assignment in enumerate(scene_assignments):
                    usage.append({
                        "scene_id": scene.get("id"),
                        "shot_index": shot_index,
                        "media_id": assignment["media_id"],
                        "source_start": assignment["source_start"],
                        "source_end": (
                            assignment["source_start"] + assignment["duration"]
                            if assignment["available_duration"] is not None
                            else None
                        ),
                        "allow_reuse": assignment["allow_reuse"],
                    })
        else:
            for scene in scenes:
                scene.pop("_incoming_review_signature", None)
                scene["approved"] = False
                scene["reviewed_media_id"] = None
                scene["reviewed_shots"] = None

        manifest["story_script"] = req.script
        manifest["scenes"] = scenes
        manifest["media_usage"] = usage
        manifest["audio_settings"] = req.audio_settings.model_dump()
        if req.master_story_brief is not None:
            manifest["master_story_brief"] = req.master_story_brief

    manifest = _update_studio_manifest(project_id, save_story)
    return {
        "story_script": manifest["story_script"],
        "master_story_brief": manifest.get("master_story_brief", ""),
        "scenes": manifest["scenes"],
        "audio_settings": manifest["audio_settings"],
    }


@app.put("/api/studio/projects/{project_id}/auto-story/input")
def save_studio_auto_story_input(project_id: str, req: StudioAutoStoryInputReq):
    def save_input(manifest: dict) -> None:
        adaptive_pacing = _studio_adaptive_pacing(manifest, req)
        manifest["master_story_brief"] = req.master_story_brief
        manifest["audio_settings"] = req.audio_settings.model_dump()
        manifest["auto_mode_settings"] = {
            "use_upload_order_fallback": req.use_upload_order_fallback,
            "adaptive_pacing": adaptive_pacing,
        }

    manifest = _update_studio_manifest(project_id, save_input)
    return {
        "master_story_brief": manifest["master_story_brief"],
        "audio_settings": manifest["audio_settings"],
        "auto_mode_settings": manifest["auto_mode_settings"],
    }


@app.post("/api/studio/projects/{project_id}/auto-story/create", status_code=202)
def create_studio_auto_story(project_id: str, req: StudioAutoStoryReq):
    manifest = _studio_manifest(project_id)
    if manifest.get("id") != project_id:
        raise HTTPException(404, "project not found")
    req = req.model_copy(update={
        "render_settings": _studio_render_settings(manifest, req.render_settings),
    })
    brief = req.master_story_brief.strip()
    if not brief:
        raise HTTPException(400, "Enter a Master Story Brief before creating a video")
    adaptive_pacing = _studio_adaptive_pacing(manifest, req)

    with STUDIO_RENDER_LOCK:
        if any(job.project_id == project_id and job.status in ("queued", "running")
               for job in STUDIO_RENDER_JOBS.values()):
            raise HTTPException(409, "A render is already queued or running for this project")

    media_paths = _validated_project_media(project_id, manifest)
    media = manifest.get("media", [])
    if not isinstance(media, list) or len(media) != len(media_paths):
        raise HTTPException(400, "Project media list is invalid")
    existing_scenes = manifest.get("scenes", [])
    if not isinstance(existing_scenes, list) or any(
        not isinstance(scene, dict) for scene in existing_scenes
    ):
        raise HTTPException(400, "Project story data is invalid")
    try:
        parsed = parse_master_story_brief(brief)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if len(parsed["scenes"]) > MAX_STUDIO_MEDIA_COUNT:
        raise HTTPException(
            400, f"A story can contain at most {MAX_STUDIO_MEDIA_COUNT} scenes"
        )
    plan = build_auto_story_plan(
        parsed, media, req.render_settings.seconds_per_scene,
        req.use_upload_order_fallback, existing_scenes,
    )
    if plan["ready"]:
        try:
            plan["media_usage"] = _studio_record_media_usage(
                project_id, plan["scenes"], media,
            )
        except HTTPException as error:
            plan["errors"].append(error.detail)
            plan["ready"] = False
    ordering = parsed["fields"]["ordering"].strip()
    ordering_note = (
        "Applied the numeric Ordering list."
        if parsed["ordering_applied"] else
        "Ordering guidance is retained but not interpreted; scene order follows the brief."
        if ordering else
        "Scene order follows the numbered instructions or sequence in the brief."
    )
    reviewed_manual = [
        scene for scene in existing_scenes
        if isinstance(scene, dict)
        and scene.get("assignment_source") == "manual"
        and scene.get("approved") is True
        and scene.get("reviewed_media_id") == scene.get("media_id")
    ] if isinstance(existing_scenes, list) else []
    conflicts = []
    if reviewed_manual:
        for old_scene in reviewed_manual:
            preserved = next((
                scene for scene in plan["scenes"]
                if scene["text"] == old_scene.get("text")
                and scene["media_id"] == old_scene.get("media_id")
                and scene["assignment_source"] == "manual"
            ), None)
            if not preserved:
                conflicts.append(
                    "The new brief changes a scene with a reviewed manual media assignment. "
                    "Enable “Replace reviewed manual assignments” to proceed."
                )
                break

    def save_input(current: dict) -> None:
        effective_adaptive_pacing = _studio_adaptive_pacing(current, req)
        current["master_story_brief"] = brief
        current["audio_settings"] = req.audio_settings.model_dump()
        current["auto_mode_settings"] = {
            "use_upload_order_fallback": req.use_upload_order_fallback,
            "adaptive_pacing": effective_adaptive_pacing,
        }

    manifest = _update_studio_manifest(project_id, save_input)
    if conflicts and not req.replace_reviewed_assignments:
        return {
            "status": "needs_input",
            "input_required": conflicts,
            "plan": [],
            "ordering_note": ordering_note,
        }
    if plan["scenes"] and (
        len(plan["scenes"]) * req.render_settings.seconds_per_scene
        > MAX_STUDIO_TOTAL_SECONDS
    ):
        plan["errors"].append(
            f"Requested scene durations exceed the {MAX_STUDIO_TOTAL_SECONDS}-second render limit."
        )
        plan["ready"] = False

    media_by_id = {item["id"]: item for item in media}
    public_plan = [
        {
            "scene_number": scene["scene_number"],
            "text": scene["text"],
            "asset_name": (
                " · ".join(
                    f"{media_by_id.get(shot['media_id'], {}).get('name', 'Missing asset')} "
                    f"({shot['start']:.2f}–{shot['start'] + shot['duration']:.2f}s)"
                    for shot in scene.get("shots", [])
                )
                if scene.get("shots") else
                media_by_id.get(scene["media_id"], {}).get("name")
            ),
            "asset_id": scene["media_id"],
            "basis": scene["match_basis"] or "needs input",
            "details": scene["match_details"],
            "duration": scene["duration"],
        }
        for scene in plan["scenes"]
    ]
    input_required = list(plan["errors"])
    input_required.extend(
        f"Scene {scene['scene_number']} has no unambiguous asset. Add an explicit "
        "asset reference or saved metadata, or choose the upload-order fallback."
        for scene in plan["scenes"] if not scene["media_id"]
    )
    if not plan["ready"]:
        return {
            "status": "needs_input",
            "input_required": input_required,
            "plan": public_plan,
            "scenes": plan["scenes"],
            "ordering_note": ordering_note,
        }

    if req.preview_only:
        return {
            "status": "preview",
            "plan": public_plan,
            "scenes": plan["scenes"],
            "ordering_note": ordering_note,
        }

    def save_plan(current: dict) -> None:
        current["story_script"] = "\n".join(
            scene["text"] for scene in plan["scenes"]
        )
        current["scenes"] = plan["scenes"]
        current["media_usage"] = plan.get("media_usage", [])

    _update_studio_manifest(project_id, save_plan)
    render_settings = req.render_settings.model_copy(
        update={"adaptive_pacing": adaptive_pacing},
    )
    job = render_studio_project(project_id, render_settings)
    return {
        "status": "rendering",
        "plan": public_plan,
        "ordering_note": ordering_note,
        "job": job,
    }


@app.post("/api/studio/projects/{project_id}/auto-director/create", status_code=202)
def auto_create_studio_video(project_id: str, req: StudioRenderReq):
    manifest = _studio_manifest(project_id)
    if manifest.get("id") != project_id:
        raise HTTPException(404, "project not found")
    req = _studio_render_settings(manifest, req)
    story_script = manifest.get("story_script", "")
    if not isinstance(story_script, str):
        raise HTTPException(400, "Project story script is invalid")
    if story_script.strip() and not manifest.get("scenes"):
        blocks = _studio_scene_blocks(story_script, req.seconds_per_scene)

        def build_scenes(current: dict) -> None:
            if not current.get("scenes"):
                current["scenes"] = blocks

        manifest = _update_studio_manifest(project_id, build_scenes)

    scenes = manifest.get("scenes", [])
    media = manifest.get("media", [])
    if (not isinstance(scenes, list) or not isinstance(media, list)
            or any(not isinstance(item, dict) for item in media)):
        raise HTTPException(400, "Project story or media data is invalid")
    if not scenes:
        job = render_studio_project(project_id, req)
        return {"status": "rendering", "scenes": [], "suggestions": [],
                "review_scene_ids": [], "job": job}
    if any(not isinstance(scene, dict) for scene in scenes):
        raise HTTPException(400, "Project contains invalid story scene data")

    suggestions = suggest_scenes(scenes, media)
    result: dict = {}

    def assign_confident(current: dict) -> None:
        current_scenes = current.get("scenes", [])
        if not isinstance(current_scenes, list) or any(
            not isinstance(scene, dict) for scene in current_scenes
        ):
            raise HTTPException(400, "Project contains invalid story scene data")
        arranged = auto_assign_confident_scenes(current_scenes, suggestions)
        media_items = current.get("media", [])
        if not isinstance(media_items, list):
            raise HTTPException(400, "Project media list is invalid")
        current["media_usage"] = _studio_record_media_usage(
            project_id, arranged, media_items,
        )
        review_scene_ids = [
            scene["id"] for scene in arranged
            if not scene.get("media_id") or not scene.get("approved", False)
        ]
        current["scenes"] = arranged
        result["scenes"] = arranged
        result["review_scene_ids"] = review_scene_ids

    _update_studio_manifest(project_id, assign_confident)
    result["suggestions"] = suggestions
    if result["review_scene_ids"]:
        result["status"] = "needs_review"
        return result
    result["status"] = "rendering"
    result["job"] = render_studio_project(project_id, req)
    return result


@app.post("/api/studio/projects/{project_id}/auto-director/resume", status_code=202)
def resume_studio_auto_director(
    project_id: str, req: StudioAutoDirectorResumeReq
):
    manifest = _studio_manifest(project_id)
    if manifest.get("id") != project_id:
        raise HTTPException(404, "project not found")
    req = req.model_copy(update={
        "render_settings": _studio_render_settings(manifest, req.render_settings),
    })
    scenes = manifest.get("scenes", [])
    media = manifest.get("media", [])
    if (not isinstance(scenes, list) or not isinstance(media, list)
            or any(not isinstance(scene, dict) for scene in scenes)
            or any(not isinstance(item, dict) for item in media)):
        raise HTTPException(400, "Project story or media data is invalid")
    def apply_confirmed(current: dict) -> None:
        current_scenes = current.get("scenes", [])
        if not isinstance(current_scenes, list):
            raise HTTPException(400, "Project story data is invalid")
        scenes_by_id = {
            scene.get("id"): scene for scene in current_scenes
            if isinstance(scene, dict)
        }
        if set(req.assignments) - set(scenes_by_id):
            raise HTTPException(400, "Review refers to a scene outside this project")
        if set(req.assignments) != set(scenes_by_id):
            raise HTTPException(409, "Review every scene before continuing")
        media_ids = {
            item.get("id") for item in current.get("media", [])
            if isinstance(item, dict)
        }
        for scene_id, scene in scenes_by_id.items():
            media_id = req.assignments[scene_id]
            if not media_id or media_id not in media_ids:
                raise HTTPException(400, "Review refers to media outside this project")
            if scene.get("media_id") != media_id:
                raise HTTPException(
                    409, "Save the current scene assignments before continuing"
                )
            if (scene.get("approved") is not True
                    or scene.get("reviewed_media_id") != media_id):
                raise HTTPException(
                    409, "Review and approve the current clip assignment for every scene"
                )

    manifest = _update_studio_manifest(project_id, apply_confirmed)
    job = render_studio_project(project_id, req.render_settings)
    return {
        "status": "rendering",
        "scenes": manifest["scenes"],
        "suggestions": suggest_scenes(manifest["scenes"], manifest.get("media", [])),
        "review_scene_ids": [],
        "job": job,
    }


@app.post("/api/studio/projects/{project_id}/auto-director/arrange")
def arrange_studio_scenes(project_id: str, req: StudioArrangeReq):
    def arrange(manifest: dict) -> None:
        scenes = manifest.get("scenes", [])
        media = manifest.get("media", [])
        if not isinstance(scenes, list) or not isinstance(media, list):
            raise HTTPException(400, "Project story or media data is invalid")
        if not scenes:
            raise HTTPException(400, "Build and save story scenes before Auto Arrange")
        if any(isinstance(scene, dict) and scene.get("media_id") for scene in scenes):
            if not req.confirm_overwrite:
                raise HTTPException(
                    409, "Existing scene assignments need explicit confirmation before Auto Arrange"
                )
        if any(not isinstance(scene, dict) for scene in scenes):
            raise HTTPException(400, "Project contains invalid story scene data")
        suggestions = suggest_scenes(scenes, media)
        arranged = arrange_scenes(scenes, suggestions)
        manifest["media_usage"] = _studio_record_media_usage(
            project_id, arranged, media,
        )
        manifest["scenes"] = arranged
        result["suggestions"] = suggestions
        result["scenes"] = manifest["scenes"]

    result: dict = {}
    _update_studio_manifest(project_id, arrange)
    return result


@app.post("/api/studio/projects/{project_id}/media")
async def upload_studio_media(project_id: str, file: UploadFile = File(...)):
    manifest = _studio_manifest(project_id)
    project_media = manifest.get("media", [])
    if not isinstance(project_media, list) or any(
        not isinstance(item, dict) for item in project_media
    ):
        raise HTTPException(400, "Project media list is invalid")
    if len(project_media) >= MAX_STUDIO_MEDIA_COUNT:
        raise HTTPException(
            400, f"A project can contain at most {MAX_STUDIO_MEDIA_COUNT} media files"
        )
    existing_sizes = [
        item.get("size", 0) for item in project_media
    ]
    if any(isinstance(size, bool) or not isinstance(size, int) or size < 0
           for size in existing_sizes):
        raise HTTPException(400, "Project contains invalid media sizes")
    project_bytes = sum(existing_sizes)
    original_name = (file.filename or "upload").replace("\\", "/").rsplit("/", 1)[-1]
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_MEDIA:
        raise HTTPException(400, "Only JPG, PNG, WEBP, and MP4 files are supported")

    media_id = uuid.uuid4().hex
    saved_name = f"{media_id}{extension}"
    destination = _studio_project_dir(project_id) / saved_name
    total = 0
    header = b""
    try:
        with destination.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "File exceeds the 200 MB upload limit")
                if project_bytes + total > MAX_STUDIO_PROJECT_BYTES:
                    raise HTTPException(413, "Project exceeds the 2 GB upload limit")
                if len(header) < 12:
                    header += chunk[:12 - len(header)]
                target.write(chunk)
        valid_content = (
            (extension in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff"))
            or (extension == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
            or (extension == ".webp" and header[:4] == b"RIFF" and header[8:12] == b"WEBP")
            or (extension == ".mp4" and header[4:8] == b"ftyp")
        )
        if not valid_content:
            raise HTTPException(400, "File contents do not match the selected extension")

        technical_metadata, metadata_error = _studio_technical_metadata(
            destination, extension
        )
        if metadata_error and "dimensions exceed the Studio limit" in metadata_error:
            raise HTTPException(400, metadata_error)
        if metadata_error:
            technical_metadata["extraction_error"] = metadata_error
        thumbnail_file = None
        if extension == ".mp4":
            thumbnail_file, thumbnail_error = _studio_video_thumbnail(
                destination, destination.parent, media_id,
                technical_metadata.get("duration_seconds"),
            )
            if thumbnail_error:
                technical_metadata["thumbnail_error"] = thumbnail_error

        clean_name = re.sub(r"[\x00-\x1f\x7f]", "", original_name).strip()
        if not clean_name:
            clean_name = f"upload{extension}"
        media_item = {
            "id": media_id,
            "name": clean_name[:180],
            "extension": extension,
            "size": total,
            "file": saved_name,
            "description": "",
            "keywords": [],
            "characters": [],
            "location": "",
            "mood": "",
            "technical_metadata": technical_metadata,
            "thumbnail_file": thumbnail_file,
        }
        def add_media(current: dict) -> None:
            current.setdefault("media", []).append(media_item)
        _update_studio_manifest(project_id, add_media)
        return _studio_media_public(project_id, media_item)
    except Exception:
        destination.unlink(missing_ok=True)
        (destination.parent / f"{media_id}.thumb.jpg").unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@app.get("/api/studio/media/{project_id}/{media_id}")
def studio_media(project_id: str, media_id: str):
    manifest = _studio_manifest(project_id)
    if not re.fullmatch(r"[a-f0-9]{32}", media_id):
        raise HTTPException(404, "media not found")
    item = next((entry for entry in manifest.get("media", [])
                 if isinstance(entry, dict) and entry.get("id") == media_id), None)
    if not item or item.get("extension") not in ALLOWED_MEDIA:
        raise HTTPException(404, "media not found")
    if item.get("file") != f"{media_id}{item['extension']}":
        raise HTTPException(404, "media not found")
    media_path = (_studio_project_dir(project_id) / item["file"]).resolve()
    if not media_path.is_relative_to(_studio_project_dir(project_id)) or not media_path.is_file():
        raise HTTPException(404, "media not found")
    mime = "video/mp4" if item["extension"] == ".mp4" else {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
    }[item["extension"]]
    return FileResponse(media_path, media_type=mime)


@app.get("/api/studio/media/{project_id}/{media_id}/thumbnail")
def studio_media_thumbnail(project_id: str, media_id: str):
    manifest = _studio_manifest(project_id)
    if not re.fullmatch(r"[a-f0-9]{32}", media_id):
        raise HTTPException(404, "thumbnail not found")
    item = next((entry for entry in manifest.get("media", [])
                 if isinstance(entry, dict) and entry.get("id") == media_id), None)
    if not item or item.get("thumbnail_file") != f"{media_id}.thumb.jpg":
        raise HTTPException(404, "thumbnail not found")
    project_dir = _studio_project_dir(project_id)
    thumbnail = (project_dir / item["thumbnail_file"]).resolve()
    if not thumbnail.is_relative_to(project_dir) or not thumbnail.is_file():
        raise HTTPException(404, "thumbnail not found")
    return FileResponse(thumbnail, media_type="image/jpeg")


@app.put("/api/studio/projects/{project_id}/media/{media_id}/metadata")
def update_studio_media_metadata(project_id: str, media_id: str,
                                 req: StudioMediaMetadataReq):
    if not re.fullmatch(r"[a-f0-9]{32}", media_id):
        raise HTTPException(404, "media not found")

    def update(manifest: dict) -> None:
        media = manifest.get("media", [])
        if not isinstance(media, list):
            raise HTTPException(400, "Project media list is invalid")
        item = next((entry for entry in media
                     if isinstance(entry, dict) and entry.get("id") == media_id), None)
        if not item:
            raise HTTPException(404, "media not found")
        metadata = req.model_dump()
        metadata["keywords"] = [value.strip() for value in metadata["keywords"] if value.strip()]
        metadata["characters"] = [value.strip() for value in metadata["characters"] if value.strip()]
        for key in ("description", "location", "mood"):
            metadata[key] = metadata[key].strip()
        item.update(metadata)

    manifest = _update_studio_manifest(project_id, update)
    item = next(item for item in manifest["media"] if item["id"] == media_id)
    return _studio_media_public(project_id, item)


@app.put("/api/studio/projects/{project_id}/media/order")
def reorder_studio_media(project_id: str, req: StudioOrderReq):
    def reorder(manifest: dict) -> None:
        media = manifest.get("media", [])
        if not isinstance(media, list) or any(not isinstance(item, dict) for item in media):
            raise HTTPException(400, "Project media list is invalid")
        current_ids = [item.get("id") for item in media]
        if len(req.media_ids) != len(current_ids) or set(req.media_ids) != set(current_ids):
            raise HTTPException(400, "Order must include every project media item exactly once")
        by_id = {item["id"]: item for item in media}
        manifest["media"] = [by_id[media_id] for media_id in req.media_ids]
    _update_studio_manifest(project_id, reorder)
    return {"ok": True}


@app.delete("/api/studio/projects/{project_id}/media/{media_id}")
def remove_studio_media(project_id: str, media_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", media_id):
        raise HTTPException(404, "media not found")
    removed = {}
    def remove(manifest: dict) -> None:
        media = manifest.get("media", [])
        if not isinstance(media, list):
            raise HTTPException(400, "Project media list is invalid")
        item = next((entry for entry in media
                     if isinstance(entry, dict) and entry.get("id") == media_id), None)
        if not item:
            raise HTTPException(404, "media not found")
        if item.get("file") != f"{media_id}{item.get('extension', '')}":
            raise HTTPException(400, "Project contains invalid media metadata")
        removed["item"] = item
        manifest["media"] = [entry for entry in media
                             if not isinstance(entry, dict) or entry.get("id") != media_id]
        for scene in manifest.get("scenes", []):
            if not isinstance(scene, dict):
                continue
            scene_shots = scene.get("shots", [])
            affected = scene.get("media_id") == media_id or (
                isinstance(scene_shots, list) and any(
                    isinstance(shot, dict) and shot.get("media_id") == media_id
                    for shot in scene_shots
                )
            )
            if scene.get("media_id") == media_id:
                scene["media_id"] = None
            shots = scene.get("shots")
            if isinstance(shots, list):
                scene["shots"] = [
                    shot for shot in shots
                    if not isinstance(shot, dict) or shot.get("media_id") != media_id
                ]
                if scene.get("media_id") is None and scene["shots"]:
                    first_shot = scene["shots"][0]
                    if isinstance(first_shot, dict):
                        scene["media_id"] = first_shot.get("media_id")
            if affected:
                scene["approved"] = False
                scene["reviewed_media_id"] = None
                scene["reviewed_shots"] = None
    _update_studio_manifest(project_id, remove)
    item = removed["item"]
    media_path = (_studio_project_dir(project_id) / item["file"]).resolve()
    if media_path.is_relative_to(_studio_project_dir(project_id)):
        media_path.unlink(missing_ok=True)
    if item.get("thumbnail_file") == f"{media_id}.thumb.jpg":
        (_studio_project_dir(project_id) / item["thumbnail_file"]).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/studio/projects/{project_id}/render", status_code=202)
def render_studio_project(project_id: str, req: StudioRenderReq):
    manifest = _studio_manifest(project_id)
    if manifest.get("id") != project_id:
        raise HTTPException(404, "project not found")
    req = _studio_render_settings(manifest, req)
    profile = get_output_profile(req.aspect_ratio)
    clips = _validated_project_media(project_id, manifest)
    if not clips:
        raise HTTPException(400, "Upload at least one clip or image before rendering")

    script = manifest.get("story_script", "")
    scenes = manifest.get("scenes", [])
    if not isinstance(script, str) or not isinstance(scenes, list):
        raise HTTPException(400, "Project story data is invalid")
    if script.strip() and not scenes:
        raise HTTPException(400, "Build and save scene blocks from the story script before rendering")
    try:
        audio_settings = StudioAudioSettings.model_validate(
            manifest.get("audio_settings", {})
        )
    except (ValidationError, TypeError) as error:
        raise HTTPException(400, "Project audio and caption settings are invalid") from error
    if (audio_settings.voiceover_enabled or audio_settings.captions_enabled) and not scenes:
        raise HTTPException(400, "Build and save story scenes before enabling voiceover or captions")
    if req.transition == "crossfade" and (
        audio_settings.voiceover_enabled or audio_settings.captions_enabled
    ):
        raise HTTPException(
            400,
            "Crossfade is disabled when voiceover or captions are enabled because "
            "scene overlap would desynchronize narration or caption timing",
        )
    if req.transition == "crossfade" and any(
        isinstance(scene, dict) and scene.get("intended_action")
        for scene in scenes
    ):
        raise HTTPException(
            400,
            "Crossfade is disabled for scenes with intended-action notes; use hard cuts "
            "to keep action boundaries clear",
        )
    if req.transition == "crossfade" and any(
        isinstance(scene, dict)
        and isinstance(scene.get("shots"), list)
        and len(scene["shots"]) > 1
        for scene in scenes
    ):
        raise HTTPException(
            400,
            "Crossfade is unavailable for multi-shot scenes because overlap shortens "
            "their fixed timeline; use hard cuts.",
        )

    scene_durations = None
    scene_data = None
    scene_visuals = None
    if scenes:
        media_items = manifest.get("media", [])
        if not isinstance(media_items, list) or len(media_items) != len(clips):
            raise HTTPException(400, "Project media list is invalid")
        scene_durations = []
        scene_data = []
        for scene in scenes:
            if not isinstance(scene, dict):
                raise HTTPException(400, "Project contains invalid story scene data")
            if not scene.get("approved", True):
                raise HTTPException(
                    400, "Review and approve every scene assignment before rendering"
                )
            if (scene.get("shots")
                    and scene.get("reviewed_shots")
                    != _studio_shot_review_signature(scene)):
                raise HTTPException(
                    400, "Review and approve every shot assignment before rendering"
                )
            media_id = scene.get("media_id")
            if not scene.get("shots") and media_id not in {
                item.get("id") for item in media_items
            }:
                raise HTTPException(400, "Assign an uploaded clip or image to every story scene")
            duration = scene.get("duration")
            if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                    or not math.isfinite(duration) or not 0.5 <= duration <= 600):
                raise HTTPException(400, "Project contains an invalid scene duration")
            text = scene.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 5000:
                raise HTTPException(400, "Project contains invalid scene text")
            scene_durations.append(float(duration))
            scene_data.append({"text": text})
        scene_visuals = _studio_scene_shot_groups(
            scenes, media_items, clips, req.fps,
        )
        clips = [
            shot["path"] for group in scene_visuals for shot in group
        ]
        if sum(scene_durations) > MAX_STUDIO_TOTAL_SECONDS:
            raise HTTPException(
                400, f"Story timeline exceeds the {MAX_STUDIO_TOTAL_SECONDS}-second render limit"
            )
    else:
        requested_total = len(clips) * req.seconds_per_scene
        if requested_total > MAX_STUDIO_TOTAL_SECONDS:
            raise HTTPException(
                400,
                f"Media-only render exceeds the {MAX_STUDIO_TOTAL_SECONDS}-second limit",
            )

    media_usage = []
    if scene_visuals is not None:
        for scene, group in zip(scenes, scene_visuals):
            for shot_index, shot in enumerate(group):
                media_usage.append({
                    "scene_id": scene.get("id"),
                    "shot_index": shot_index,
                    "media_id": shot["media_id"],
                    "source_start": shot["source_start"],
                    "source_end": (
                        shot["source_start"] + shot["duration"]
                        if shot["available_duration"] is not None else None
                    ),
                    "allow_reuse": shot["allow_reuse"],
                })
    else:
        media_items = manifest.get("media", [])
        media_only_scenes = [
            {"media_id": item["id"], "duration": req.seconds_per_scene}
            for item in media_items
        ]
        media_only_assignments = _studio_source_assignments(
            media_only_scenes, media_items, clips, req.fps,
        )
        for index, group in enumerate(media_only_assignments):
            for shot in group:
                media_usage.append({
                    "scene_id": None,
                    "shot_index": index,
                    "media_id": shot["media_id"],
                    "source_start": shot["source_start"],
                    "source_end": (
                        shot["source_start"] + shot["duration"]
                        if shot["available_duration"] is not None
                        else None
                    ),
                    "allow_reuse": shot["allow_reuse"],
                })

    with STUDIO_RENDER_LOCK:
        if any(job.project_id == project_id and job.status in ("queued", "running")
               for job in STUDIO_RENDER_JOBS.values()):
            raise HTTPException(409, "A render is already queued or running for this project")
        def persist_render_settings(current: dict) -> None:
            if current.get("scenes", []) != scenes:
                raise HTTPException(
                    409, "The story changed during render validation. Save and try again."
                )
            current["quality_settings"] = {
                "aspect_ratio": req.aspect_ratio,
                "image_motion": req.image_motion,
                "transition": req.transition,
                "export_preset": req.export_preset,
            }
            current["media_usage"] = media_usage
            if scene_visuals is not None:
                current_scenes = current.get("scenes", [])
                for scene, group in zip(current_scenes, scene_visuals):
                    scene_shots = scene.get("shots", [])
                    if isinstance(scene_shots, list):
                        for scene_shot, resolved in zip(scene_shots, group):
                            scene_shot["source_start"] = resolved["source_start"]

        _update_studio_manifest(project_id, persist_render_settings)
        job = StudioRenderJob(uuid.uuid4().hex, project_id, req, audio_settings)
        estimate_durations = (
            scene_durations if scene_durations is not None
            else [req.seconds_per_scene] * len(clips)
        )
        job.estimated_duration = sum(
            math.ceil(duration * req.fps - 1e-9) / req.fps
            for duration in estimate_durations
        )
        STUDIO_RENDER_JOBS[job.id] = job
        worker = threading.Thread(
            target=_render_studio_project,
            args=(job, clips, scene_durations, scene_data, scene_visuals), daemon=True,
            name=f"studio-render-{job.id[:8]}",
        )
        try:
            worker.start()
        except RuntimeError as error:
            job.status = "failed"
            job.error = str(error)
            job.finished = datetime.now().isoformat(timespec="seconds")
            raise HTTPException(500, "Could not start render worker") from error
    return {
        **job.as_dict(),
        "width": profile.width,
        "height": profile.height,
    }


@app.get("/api/studio/projects/{project_id}/render")
def studio_render_status(project_id: str):
    manifest = _studio_manifest(project_id)
    if manifest.get("id") != project_id:
        raise HTTPException(404, "project not found")
    with STUDIO_RENDER_LOCK:
        jobs = [job for job in STUDIO_RENDER_JOBS.values()
                if job.project_id == project_id]
        if not jobs:
            return {"status": "idle"}
        latest = jobs[-1]
        return latest.as_dict()


@app.get("/api/studio/projects/{project_id}/exports/{export_id}/video")
def studio_export_video(project_id: str, export_id: str):
    return FileResponse(_studio_export_file(project_id, export_id), media_type="video/mp4")


@app.get("/api/studio/projects/{project_id}/exports/{export_id}/download")
def studio_export_download(project_id: str, export_id: str):
    return FileResponse(
        _studio_export_file(project_id, export_id),
        media_type="video/mp4",
        filename=f"video-{export_id[:8]}.mp4",
    )


@app.get("/api/studio/exports")
def studio_exports():
    items = []
    for item in library():
        if item["has_video"]:
            items.append({
                **item,
                "video_url": f"/api/video/{item['rel']}",
                "download_url": f"/api/download/{item['rel']}",
            })
    if STUDIO_PROJECTS.exists():
        for manifest_path in STUDIO_PROJECTS.glob("*/project.json"):
            try:
                project_id = manifest_path.parent.name
                manifest = _studio_manifest(project_id)
                if manifest.get("id") != project_id:
                    continue
                for export in manifest.get("exports", []):
                    try:
                        _studio_export_file(project_id, export["id"])
                    except (HTTPException, KeyError):
                        continue
                    items.append({
                        "id": export["id"],
                        "project_id": project_id,
                        "title": export.get("name", "Studio export"),
                        "channel": manifest.get("name", "Studio project"),
                        "date": export.get("created", ""),
                        "duration": export.get("duration"),
                        "video_url": f"/api/studio/projects/{project_id}/exports/{export['id']}/video",
                        "download_url": f"/api/studio/projects/{project_id}/exports/{export['id']}/download",
                    })
            except (HTTPException, OSError, json.JSONDecodeError):
                continue
    return items


@app.get("/api/download/{rel:path}")
def download_video(rel: str):
    video_path = (OUTPUT / rel / "final.mp4").resolve()
    if not video_path.is_relative_to(OUTPUT.resolve()) or not video_path.is_file():
        raise HTTPException(404, "video not found")
    return FileResponse(video_path, media_type="video/mp4", filename="video.mp4")


# ── analytics ────────────────────────────────────────────────────────────────
@app.get("/api/analytics")
def analytics():
    hf = OUTPUT / "history.json"
    hist = json.loads(hf.read_text(encoding="utf-8")) if hf.exists() else {}
    total = sum(len(v.get("topics", [])) for v in hist.values())
    runs = []
    rf = OUTPUT / "runs.jsonl"
    if rf.exists():
        for ln in rf.read_text(encoding="utf-8").splitlines()[-100:]:
            try:
                runs.append(json.loads(ln))
            except Exception:
                pass
    return {
        "total_videos": total,
        "channels": {k: len(v.get("topics", [])) for k, v in hist.items()},
        "recent_runs": list(reversed(runs)),
    }


# ── static ───────────────────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
