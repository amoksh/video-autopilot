"""Deterministic parsing and asset planning for Studio Auto Mode."""
from __future__ import annotations

import re
import uuid

from src.studio_matching import rank_clips_for_scene


_LABELS = {
    "story": "story",
    "narration": "narration",
    "characters": "characters",
    "location": "location",
    "locations": "location",
    "visual": "visual",
    "visual direction": "visual",
    "visual directions": "visual",
    "mood": "mood",
    "keywords": "keywords",
    "ordering": "ordering",
    "desired ordering": "ordering",
}
_LABEL_RE = re.compile(
    r"^\s*(Story|Narration|Characters?|Locations?|Visual(?:\s+Directions?)?"
    r"|Mood|Keywords|Desired\s+Ordering|Ordering|Action|Direction|"
    r"Asset|Clip|Media|File(?:name)?)\s*:\s*(.*)$",
    re.IGNORECASE,
)
_SCENE_RE = re.compile(
    r"^\s*(?:scene\s+)?(\d+)\s*(?:[):\-]\s*|\.\s+)(.*)$",
    re.IGNORECASE,
)
_ASSET_RE = re.compile(
    r"\b(?:asset|clip|media|image|video)\s*"
    r"(?::\s*|#|no\.?\s*|number\s*)?(\d+)\b",
    re.IGNORECASE,
)
_INLINE_LABEL_RE = re.compile(
    r"(?:^|[|;])\s*(Visual(?:\s+Directions?)?|Characters?|Locations?|Mood|Keywords|"
    r"Action|Direction|Narration|Asset|Clip|Media|File(?:name)?|"
    r"Story)\s*:\s*",
    re.IGNORECASE,
)


def _field_key(label: str) -> str:
    normalized = " ".join(label.casefold().split())
    return {
        "story": "story",
        "narration": "narration",
        "character": "characters",
        "characters": "characters",
        "location": "location",
        "locations": "location",
        "visual": "visual",
        "visual direction": "visual",
        "visual directions": "visual",
        "mood": "mood",
        "keywords": "keywords",
        "desired ordering": "ordering",
        "ordering": "ordering",
        "action": "action",
        "direction": "action",
        "asset": "asset",
        "clip": "asset",
        "media": "asset",
        "file": "asset",
        "filename": "asset",
    }[normalized]


def _terms(value: str) -> list[str]:
    return [term.strip() for term in re.split(r"[,;\n]", value) if term.strip()]


def _sentences(value: str) -> list[str]:
    result: list[str] = []
    for paragraph in re.split(r"\n\s*\n", value.strip()):
        for line in paragraph.splitlines():
            result.extend(
                sentence.strip()
                for sentence in re.split(r"(?<=[.!?])\s+", line.strip())
                if sentence.strip()
            )
    return result


def parse_master_story_brief(brief: str) -> dict:
    """Parse scene blocks, including continuation fields, without inferring semantics."""
    fields = {key: "" for key in set(_LABELS.values())}
    free_text: list[str] = []
    numbered: list[dict] = []
    current_scene: dict | None = None
    current_field: str | None = None

    def append_scene_line(line: str) -> None:
        if current_scene is not None and line.strip():
            current_scene["lines"].append(line.strip())

    def append_top_level(line: str) -> None:
        nonlocal current_field
        if not line.strip():
            if current_field and fields.get(current_field):
                fields[current_field] += "\n\n"
            return
        label_match = _LABEL_RE.match(line)
        if label_match:
            key = _field_key(label_match.group(1))
            if key not in fields:
                free_text.append(line.strip())
                current_field = None
                return
            value = label_match.group(2).strip()
            fields[key] = "\n".join(filter(None, (fields[key], value)))
            current_field = key
        elif current_field:
            fields[current_field] = "\n".join(
                filter(None, (fields[current_field], line.strip()))
            )
        else:
            free_text.append(line.strip())

    for line in brief.splitlines():
        scene_match = _SCENE_RE.match(line)
        if scene_match:
            if len(scene_match.group(1)) > 6:
                raise ValueError("Scene numbers must contain six digits or fewer")
            if int(scene_match.group(1)) == 0:
                raise ValueError("Scene numbers must start at 1")
            current_scene = {
                "number": int(scene_match.group(1)),
                "lines": [],
            }
            numbered.append(current_scene)
            append_scene_line(scene_match.group(2))
            current_field = None
            continue
        if current_scene is not None:
            append_scene_line(line)
        else:
            append_top_level(line)

    if not numbered:
        narrative = fields["narration"] or fields["story"] or "\n".join(free_text)
        numbered = [
            {"number": index + 1, "lines": [text]}
            for index, text in enumerate(_sentences(narrative))
        ]

    ordering = fields["ordering"].strip()
    ordering_applied = False
    counts: dict[int, int] = {}
    for scene in numbered:
        counts[scene["number"]] = counts.get(scene["number"], 0) + 1
    duplicate_scene_numbers = sorted(
        number for number, count in counts.items() if count > 1
    )
    source_scenes = numbered
    if ordering and source_scenes:
        order_values = re.findall(r"\b(?:scene\s*)?(\d+)\b", ordering, re.I)
        requested_numbers = [
            int(value) for value in order_values if len(value) <= 6
        ]
        by_number = {scene["number"]: scene for scene in source_scenes}
        if (len(requested_numbers) == len(source_scenes)
                and len(set(requested_numbers)) == len(requested_numbers)
                and set(requested_numbers) == set(by_number)):
            source_scenes = [by_number[number] for number in requested_numbers]
            ordering_applied = True

    parsed_scenes = []
    global_chars = _terms(fields["characters"])
    for source in source_scenes:
        scene_fields: dict[str, list[str]] = {}
        prose: list[str] = []
        current_scene_field: str | None = None
        instruction = "\n".join(source["lines"])
        for line in source["lines"]:
            parts = _INLINE_LABEL_RE.split(line)
            if len(parts) > 1:
                if parts[0].strip():
                    prose.append(parts[0].strip(" \t|;:-"))
                for index in range(1, len(parts), 2):
                    key = _field_key(parts[index])
                    value = parts[index + 1].strip(" \t|;")
                    if key == "asset":
                        scene_fields.setdefault(key, []).append(value)
                    elif value:
                        scene_fields.setdefault(key, []).append(value)
                    current_scene_field = key
                continue

            label_match = _LABEL_RE.match(line)
            if label_match:
                key = _field_key(label_match.group(1))
                value = label_match.group(2).strip()
                if key == "asset":
                    scene_fields.setdefault(key, []).append(value)
                elif value and key in {
                    "story", "narration", "characters", "location", "visual",
                    "mood", "keywords", "action",
                }:
                    scene_fields.setdefault(key, []).append(value)
                current_scene_field = key
                continue

            if current_scene_field in {
                "story", "narration", "characters", "location", "visual",
                "mood", "keywords", "action", "asset",
            }:
                if line.strip():
                    scene_fields.setdefault(current_scene_field, []).append(line.strip())
            elif line.strip():
                prose.append(line.strip())

        raw_asset_values = [
            value for value in scene_fields.get("asset", [])
        ]
        asset_values = _ASSET_RE.findall(instruction)
        asset_values.extend(
            value for field_value in raw_asset_values
            for value in _ASSET_RE.findall(f"Asset: {field_value}")
        )
        asset_values = list(dict.fromkeys(asset_values))
        refs = [int(value) for value in asset_values if len(value) <= 6]
        invalid_asset_references = [value for value in asset_values if len(value) > 6]
        named = [
            value.strip("\"'` ")
            for value in raw_asset_values
            if value and not re.fullmatch(r"(?:#|no\.?\s*)?\d+", value, re.I)
        ]
        narration = "\n".join(scene_fields.get("narration", [])).strip()
        if not narration:
            narration = "\n".join(scene_fields.get("story", [])).strip()
        if not narration:
            narration = " ".join(prose).strip()
        visuals = [
            *scene_fields.get("visual", []),
            *scene_fields.get("keywords", []),
        ]
        parsed_scenes.append({
            "number": source["number"],
            "text": narration,
            "raw_instruction": instruction,
            "asset_numbers": refs,
            "invalid_asset_references": invalid_asset_references,
            "asset_names": named,
            "visual_description": " ".join(
                value for value in (fields["visual"], *visuals)
                if value
            ),
            "characters": (
                _terms("\n".join(scene_fields.get("characters", []))) or global_chars
            ),
            "location": "\n".join(scene_fields.get("location", [])) or fields["location"],
            "mood": "\n".join(scene_fields.get("mood", [])) or fields["mood"],
            "intended_action": "\n".join(scene_fields.get("action", [])),
        })

    return {
        "fields": fields,
        "scenes": parsed_scenes,
        "ordering_applied": ordering_applied,
        "duplicate_scene_numbers": duplicate_scene_numbers,
    }


def build_auto_story_plan(
    parsed: dict,
    media: list[dict],
    duration: float,
    use_upload_order: bool,
    existing_scenes: list[dict] | None = None,
) -> dict:
    """Map instructions to uploads, returning explicit evidence and unresolved inputs."""
    scenes = parsed["scenes"]
    errors = [
        f"Scene number {number} is repeated; use unique scene numbers."
        for number in parsed.get("duplicate_scene_numbers", [])
    ]
    plan: list[dict] = []
    media_by_id = {item["id"]: item for item in media}
    existing = existing_scenes or []
    used: set[str] = set()
    references: list[str | None] = []

    for index, source in enumerate(scenes):
        explicit_ids: list[str] = []
        if source["invalid_asset_references"]:
            errors.append(
                f"Scene {source['number']} contains an asset reference with too many digits."
            )
        for number in source["asset_numbers"]:
            if not 1 <= number <= len(media):
                errors.append(
                    f"Scene {source['number']} references asset {number}, but only "
                    f"{len(media)} uploads exist."
                )
            else:
                explicit_ids.append(media[number - 1]["id"])
        for name in source["asset_names"]:
            matches = [
                item for item in media
                if name.casefold() in {
                    item.get("name", "").casefold(),
                    item.get("name", "").rsplit(".", 1)[0].casefold(),
                }
            ]
            if len(matches) != 1:
                errors.append(
                    f"Scene {source['number']} file reference {name!r} "
                    f"{'does not match an upload' if not matches else 'matches multiple uploads'}."
                )
            else:
                explicit_ids.append(matches[0]["id"])
        for item in media:
            filename = item.get("name", "")
            if (isinstance(filename, str) and filename
                    and filename.casefold() in source["raw_instruction"].casefold()):
                explicit_ids.append(item["id"])
        if len(set(explicit_ids)) > 1:
            errors.append(f"Scene {source['number']} has conflicting asset references.")
            explicit_ids = []
        references.append(explicit_ids[0] if explicit_ids else None)

    for index, source in enumerate(scenes):
        prior = existing[index] if index < len(existing) else {}
        prior_id = prior.get("media_id")
        prior_shots = prior.get("shots", [])
        if (not isinstance(prior_shots, list)
                or any(not isinstance(shot, dict)
                       or shot.get("media_id") not in media_by_id
                       for shot in prior_shots)):
            prior_shots = []
        try:
            has_matching_timing = (
                abs(float(prior.get("duration")) - duration) < 1e-6
                and abs(sum(float(shot["duration"]) for shot in prior_shots)
                        - duration) < 1e-6
            )
        except (TypeError, ValueError, KeyError):
            has_matching_timing = False
        if (prior.get("text") != source["text"]
                or prior.get("approved") is not True
                or not prior.get("reviewed_shots")
                or not has_matching_timing):
            prior_shots = []
        is_reviewed_manual = (
            prior.get("assignment_source") == "manual"
            and prior.get("approved") is True
            and prior.get("reviewed_media_id") == prior_id
            and prior_id in media_by_id
            and prior.get("text") == source["text"]
        )
        plan.append({
            "id": uuid.uuid4().hex,
            "scene_number": source["number"],
            "text": source["text"],
            "media_id": (
                prior_shots[0]["media_id"] if prior_shots
                else None
            ),
            "shots": prior_shots,
            "reviewed_shots": (
                prior.get("reviewed_shots") if prior_shots else None
            ),
            "reviewed_media_id": None,
            "duration": duration,
            "visual_description": source["visual_description"],
            "characters": source["characters"],
            "location": source["location"],
            "mood": source["mood"],
            "intended_action": source["intended_action"],
            "assignment_source": "auto",
            "approved": False,
            "match_basis": None,
            "match_details": "",
            "_explicit": references[index],
            "_reviewed": prior_id if is_reviewed_manual else None,
        })
        if not source["text"].strip():
            errors.append(f"Scene {source['number']} is missing narration or scene text.")
        elif len(source["text"]) > 5000:
            errors.append(f"Scene {source['number']} exceeds the 5,000-character limit.")
        if len(source["visual_description"]) > 2000:
            errors.append(f"Scene {source['number']} visual directions exceed 2,000 characters.")
        if len(source["characters"]) > 30 or any(
            len(value) > 100 for value in source["characters"]
        ):
            errors.append(f"Scene {source['number']} has too many or oversized character names.")
        if len(source["location"]) > 300 or len(source["mood"]) > 300:
            errors.append(f"Scene {source['number']} location or mood exceeds 300 characters.")
        if len(source["intended_action"]) > 1000:
            errors.append(f"Scene {source['number']} direction exceeds 1,000 characters.")

    for scene in plan:
        if scene["_explicit"]:
            if (scene["shots"]
                    and scene["shots"][0]["media_id"] != scene["_explicit"]):
                scene["shots"] = []
            scene["media_id"] = scene["_explicit"]
            if scene["_reviewed"] == scene["_explicit"]:
                scene["assignment_source"] = "manual"
                scene["reviewed_media_id"] = scene["_explicit"]
                scene["match_details"] = (
                    "The explicit asset reference agrees with the existing reviewed assignment."
                )
            else:
                scene["match_details"] = (
                    "The brief names this upload by number or exact filename."
                )
            scene["match_basis"] = "explicit mapping"
            scene["approved"] = True
        elif scene["_reviewed"]:
            scene["media_id"] = scene["_reviewed"]
            scene["assignment_source"] = "manual"
            scene["reviewed_media_id"] = scene["_reviewed"]
            scene["match_basis"] = "reviewed manual assignment"
            scene["match_details"] = "Kept because the reviewed scene text is unchanged."
            scene["approved"] = True
        if scene["media_id"]:
            used.add(scene["media_id"])

    for scene in plan:
        if scene["media_id"] or not media:
            continue
        ranked = rank_clips_for_scene(scene, media)
        winner = next((
            item for item in ranked
            if item.get("auto_assignable") and item["media_id"] not in used
        ), None)
        if winner:
            scene["media_id"] = winner["media_id"]
            scene["assignment_source"] = "auto"
            scene["match_basis"] = "saved metadata match"
            scene["match_details"] = (
                f"Heuristic text overlap with saved metadata: "
                f"{'; '.join(winner['reasons'])}. This is not visual understanding."
            )
            scene["approved"] = True
            used.add(winner["media_id"])

    unresolved = [scene for scene in plan if not scene["media_id"]]
    if use_upload_order and media:
        available = [item["id"] for item in media if item["id"] not in used]
        cursor = 0
        for scene in unresolved:
            if cursor < len(available):
                media_id = available[cursor]
            else:
                media_id = media[(len(plan) - len(unresolved) + cursor) % len(media)]["id"]
            cursor += 1
            scene["media_id"] = media_id
            scene["match_basis"] = "numbered-order fallback"
            reused = sum(item["media_id"] == media_id for item in plan) > 1
            scene["match_details"] = (
                f"Upload-order fallback selected {media_by_id[media_id]['name']}."
                + (" This upload is reused because the plan has more scenes than uploads."
                   if reused else "")
            )
            scene["approved"] = True
            used.add(media_id)
        unresolved = []

    for scene in plan:
        scene.pop("_explicit", None)
        scene.pop("_reviewed", None)
    if not scenes:
        errors.append("Add narration, story text, or numbered scene instructions.")
    if not media:
        errors.append("Upload at least one image or video before creating the story.")
    return {
        "scenes": plan,
        "unresolved_scene_ids": [scene["id"] for scene in unresolved],
        "errors": errors,
        "ready": not errors and not unresolved,
    }
