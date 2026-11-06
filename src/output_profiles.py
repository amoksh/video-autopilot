"""Supported output formats for all video render paths."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


OutputAspectRatio = Literal["9:16", "16:9"]


@dataclass(frozen=True)
class OutputProfile:
    aspect_ratio: OutputAspectRatio
    width: int
    height: int
    label: str


OUTPUT_PROFILES: dict[str, OutputProfile] = {
    "9:16": OutputProfile("9:16", 1080, 1920, "Vertical"),
    "16:9": OutputProfile("16:9", 1920, 1080, "Landscape"),
}


def get_output_profile(aspect_ratio: str = "9:16") -> OutputProfile:
    profile = OUTPUT_PROFILES.get(aspect_ratio)
    if profile is None:
        raise ValueError(f"Unsupported output aspect ratio: {aspect_ratio}")
    return profile
