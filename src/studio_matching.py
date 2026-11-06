"""Deterministic, text-only clip suggestions for Studio story scenes."""
from __future__ import annotations

import re
from typing import Any


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "into", "is", "it", "of", "on", "or", "that", "the", "their",
    "then", "there", "this", "to", "with",
}
_COMPONENTS = (
    ("description", 0.40),
    ("keywords", 0.20),
    ("characters", 0.15),
    ("location", 0.10),
    ("mood", 0.05),
    ("scene_context", 0.10),
)
_MEDIA_METADATA_FIELDS = ("description", "keywords", "characters", "location", "mood")
_MIN_MATCH_SCORE = 20.0


def _tokens(value: Any) -> set[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = [item for item in value if isinstance(item, str)]
    else:
        values = []
    return {
        normalized for value in values
        for token in _TOKEN_RE.findall(value.casefold())
        if token not in _STOP_WORDS
        for normalized in (_normalize_token(token),)
    }


def _normalize_token(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith(("ches", "shes", "xes", "zes", "ses")):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _dice(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


def _clip_fields(clip: dict) -> dict[str, set[str]]:
    fields = {
        "description": _tokens(clip.get("description")),
        "keywords": _tokens(clip.get("keywords")),
        "characters": _tokens(clip.get("characters")),
        "location": _tokens(clip.get("location")),
        "mood": _tokens(clip.get("mood")),
    }
    fields["scene_context"] = set().union(
        fields["characters"], fields["location"], fields["mood"]
    )
    return fields


def rank_clips_for_scene(scene: dict, clips: list[dict]) -> list[dict]:
    """Return stable, explainable lexical rankings; this does not inspect footage."""
    scene_description = _tokens([
        scene.get("text", ""),
        scene.get("visual_description", ""),
        scene.get("intended_action", ""),
    ])
    scene_fields = {
        "description": scene_description,
        "keywords": scene_description,
        "characters": _tokens(scene.get("characters")),
        "location": _tokens(scene.get("location")),
        "mood": _tokens(scene.get("mood")),
        "scene_context": scene_description,
    }
    ranked = []
    for order, clip in enumerate(clips):
        clip_fields = _clip_fields(clip)
        coverage = sum(bool(clip_fields[key]) for key in _MEDIA_METADATA_FIELDS)
        score = 0.0
        matched_fields = 0
        reasons = []
        for key, weight in _COMPONENTS:
            overlap = scene_fields[key] & clip_fields[key]
            component_score = 100 * weight * _dice(
                scene_fields[key], clip_fields[key]
            )
            score += component_score
            if overlap:
                if key in _MEDIA_METADATA_FIELDS:
                    matched_fields += 1
                terms = ", ".join(sorted(overlap))
                reasons.append(
                    f"{key.title()} overlap: {terms} ({component_score:.1f} pts)"
                )
        if coverage < 2 or matched_fields < 2 or score < _MIN_MATCH_SCORE:
            confidence = "uncertain"
        elif score >= 45:
            confidence = "high"
        else:
            confidence = "moderate"
        ranked.append({
            "media_id": clip.get("id"),
            "name": clip.get("name", "Unnamed clip"),
            "score": round(score, 1),
            "confidence": confidence,
            "metadata_fields": coverage,
            "matched_fields": matched_fields,
            "reasons": reasons or ["No matching metadata terms"],
            "matchable": confidence != "uncertain",
            "_order": order,
        })
    ranked.sort(key=lambda result: (-result["score"], result["_order"]))
    top_margin = (
        ranked[0]["score"] - ranked[1]["score"]
        if len(ranked) > 1 else ranked[0]["score"] if ranked else 0
    )
    for index, result in enumerate(ranked):
        result["margin"] = round(top_margin, 1)
        result["auto_assignable"] = (
            index == 0 and result["confidence"] == "high" and top_margin >= 5
        )
        del result["_order"]
    return ranked


def suggest_scenes(scenes: list[dict], clips: list[dict]) -> list[dict]:
    return [
        {
            "scene_id": scene.get("id"),
            "ranked_clips": rank_clips_for_scene(scene, clips),
        }
        for scene in scenes
    ]


def arrange_scenes(scenes: list[dict], suggestions: list[dict]) -> list[dict]:
    """Build an unapproved proposal; uncertain scenes remain unassigned."""
    by_scene = {entry.get("scene_id"): entry.get("ranked_clips", [])
                for entry in suggestions}
    arranged = []
    for scene in scenes:
        updated = dict(scene)
        ranked = by_scene.get(scene.get("id"), [])
        best = ranked[0] if ranked else None
        if best and best.get("matchable"):
            updated["media_id"] = best["media_id"]
            updated["assignment_source"] = "auto"
        else:
            updated["media_id"] = None
            updated["assignment_source"] = "auto"
        updated["approved"] = False
        arranged.append(updated)
    return arranged


def auto_assign_confident_scenes(
    scenes: list[dict], suggestions: list[dict]
) -> list[dict]:
    """Assign only clear high-confidence winners, preserving all existing choices."""
    by_scene = {
        entry.get("scene_id"): entry.get("ranked_clips", [])
        for entry in suggestions
    }
    arranged = []
    for scene in scenes:
        updated = dict(scene)
        ranked = by_scene.get(scene.get("id"), [])
        best = ranked[0] if ranked else None
        if not updated.get("media_id") and best and best.get("auto_assignable"):
            updated["media_id"] = best["media_id"]
            updated["assignment_source"] = "auto"
            updated["approved"] = True
        arranged.append(updated)
    return arranged
