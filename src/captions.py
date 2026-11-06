"""Stage 5 — word-level animated captions via faster-whisper → styled ASS.

Kinetic, word-by-word burned-in captions are the single biggest free retention lever for
muted viewing (Playbook §2). We transcribe the actual voiceover so timing is frame-accurate,
then animate the active word with a scale "pop" + color highlight for that pro, viral look.
"""
from __future__ import annotations

import re
import math
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Literal, Optional

from .output_profiles import get_output_profile
from .utils import log

# Strip markdown / stray formatting an LLM might leave in on-screen text.
_MD = re.compile(r"[*_`#~]+")


def _clean(text: str) -> str:
    return _MD.sub("", text).strip().strip('"').strip("'").strip()


def _ts(seconds: float) -> str:
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    text = text.replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\N")


def _caption_char_width(char: str) -> float:
    if unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in {"W", "F"}:
        return 1
    category = unicodedata.category(char)
    if category.startswith("P"):
        return 0.48
    if category.startswith("Z") or char.isspace():
        return 0.3
    return 0.62


def _caption_font_path(fontname: str = "DejaVu Sans") -> str:
    fc_match = shutil.which("fc-match")
    if not fc_match:
        raise RuntimeError("Caption width measurement requires fontconfig (fc-match)")
    result = subprocess.run(
        [fc_match, "-f", "%{file}", f"{fontname}:style=Bold"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    font_path = result.stdout.strip()
    if not font_path or not Path(font_path).is_file():
        raise RuntimeError(f"Could not locate the bold caption font {fontname!r}")
    return font_path


def _measure_caption_widths(lines: list[str], font_size: int,
                            fontname: str = "DejaVu Sans") -> list[float]:
    """Measure line advances with FFmpeg's FreeType renderer and caption font."""
    if not lines:
        return []
    font_path = _caption_font_path(fontname)
    with tempfile.TemporaryDirectory(prefix="va-caption-measure-") as temporary:
        directory = Path(temporary)
        filters = []
        for index, line in enumerate(lines):
            (directory / f"line-{index}.txt").write_text(line, encoding="utf-8")
            filters.append(
                "drawtext=fontfile='{}':textfile='line-{}.txt':fontsize={}:"
                "x='print(text_w+{})':y=0".format(
                    font_path, index, font_size, index * 100000,
                )
            )
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "info", "-nostats",
                "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.04:r=1",
                "-vf", ",".join(filters), "-frames:v", "1", "-f", "null", "-",
            ],
            cwd=directory, check=True, capture_output=True, text=True, timeout=30,
        )
    measurements = [
        float(line.strip())
        for line in result.stderr.splitlines()
        if re.fullmatch(r"\d+(?:\.\d+)?", line.strip())
    ]
    if len(measurements) != len(lines):
        raise RuntimeError(
            "FFmpeg did not return a width for every caption line "
            f"(expected {len(lines)}, got {len(measurements)})"
        )
    return [
        measured - index * 100000
        for index, measured in enumerate(measurements)
    ]


def _estimated_caption_width(text: str, font_size: int) -> float:
    return sum(_caption_char_width(char) for char in text) * font_size


def _split_long_word(word: str, max_line_width: float, font_size: int,
                     fontname: str) -> list[str]:
    if max_line_width <= 0:
        raise ValueError("Caption safe area is too narrow for the selected style")
    clusters: list[str] = []
    for char in word:
        if unicodedata.combining(char) and clusters:
            clusters[-1] += char
        else:
            clusters.append(char)

    pieces: list[str] = []
    while clusters:
        prefixes = [
            "".join(clusters[:end])
            for end in range(1, len(clusters) + 1)
        ]
        widths = _measure_caption_widths(prefixes, font_size, fontname)
        fitting = [
            end for end, measured in enumerate(widths, 1)
            if measured <= max_line_width
        ]
        if not fitting:
            raise ValueError(
                "A caption character cannot fit inside the horizontal safe area"
            )
        count = max(fitting)
        pieces.append(prefixes[count - 1])
        del clusters[:count]
    return pieces


def _wrap_caption_text(text: str, max_line_width: float, font_size: int,
                       fontname: str = "DejaVu Sans") -> list[str]:
    """Wrap using font metrics measured by FFmpeg's FreeType renderer."""
    if max_line_width <= 0:
        raise ValueError("Caption safe area is too narrow for the selected style")
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        proposed = f"{current} {word}".strip()
        if (current and _estimated_caption_width(proposed, font_size)
                > max_line_width * 0.92):
            lines.append(current)
            current = ""
            proposed = word
        if _estimated_caption_width(word, font_size) > max_line_width * 0.92:
            if current:
                lines.append(current)
                current = ""
            pieces = _split_long_word(word, max_line_width, font_size, fontname)
            lines.extend(pieces[:-1])
            current = pieces[-1]
            continue
        current = proposed
    if current:
        lines.append(current)

    while True:
        widths = _measure_caption_widths(lines, font_size, fontname)
        overflowing = [
            index for index, width in enumerate(widths)
            if width > max_line_width
        ]
        if not overflowing:
            break
        replacements: dict[int, list[str]] = {}
        prefix_groups: dict[int, list[str]] = {}
        all_prefixes = []
        for index in overflowing:
            parts = lines[index].split()
            if len(parts) <= 1:
                replacements[index] = _split_long_word(
                    parts[0], max_line_width, font_size, fontname,
                )
                continue
            prefixes = [" ".join(parts[:end]) for end in range(1, len(parts) + 1)]
            prefix_groups[index] = prefixes
            all_prefixes.extend(prefixes)
        prefix_widths = iter(
            _measure_caption_widths(all_prefixes, font_size, fontname)
            if all_prefixes else []
        )
        for index, prefixes in prefix_groups.items():
            measured = [next(prefix_widths) for _ in prefixes]
            fitting = [
                end for end, width in enumerate(measured, 1)
                if width <= max_line_width
            ]
            if not fitting:
                parts = lines[index].split(maxsplit=1)
                replacements[index] = _split_long_word(
                    parts[0], max_line_width, font_size, fontname,
                ) + ([parts[1]] if len(parts) == 2 else [])
            else:
                end = max(fitting)
                parts = lines[index].split()
                replacements[index] = [
                    " ".join(parts[:end]),
                    *(
                        [" ".join(parts[end:])]
                        if end < len(parts) else []
                    ),
                ]
        previous_lines = lines
        lines = [
            piece
            for index, line in enumerate(lines)
            for piece in replacements.get(index, [line])
        ]
        if lines == previous_lines:
            raise ValueError("Caption text cannot fit inside the horizontal safe area")
    return lines


def build_scene_captions(scenes: list[dict], timings: list[dict],
                         output_path: Path, width: int = 1080,
                         height: int = 1920,
                         size: Literal["small", "medium", "large"] = "medium",
                         position: Literal["lower", "middle"] = "lower") -> Path:
    """Write wrapped, scene-level ASS captions using supplied timeline timestamps."""
    if not scenes or len(scenes) != len(timings):
        raise ValueError("Caption scenes and timestamps must be non-empty and match")
    if (isinstance(width, bool) or not isinstance(width, int) or width < 1
            or isinstance(height, bool) or not isinstance(height, int) or height < 1):
        raise ValueError("Caption video dimensions must be positive")
    if not isinstance(size, str) or size not in {"small", "medium", "large"}:
        raise ValueError("Caption size must be small, medium, or large")
    if not isinstance(position, str) or position not in {"lower", "middle"}:
        raise ValueError("Caption position must be lower or middle")

    font_ratio = {"small": 0.027, "medium": 0.03125, "large": 0.0396}[size]
    font_size = max(1, round(min(height * font_ratio, width * 0.08)))
    margin_x = round(width * 0.09)
    margin_y = round(height * 270 / 1920) if position == "lower" else 0
    alignment = 2 if position == "lower" else 5
    outline = max(1, round(font_size * 0.045))
    shadow = max(1, round(font_size * 0.02))
    max_line_width = width - 2 * margin_x - 2 * (outline + shadow)
    events = []
    for index, (scene, timing) in enumerate(zip(scenes, timings), 1):
        text = scene.get("text")
        start = timing.get("start")
        end = timing.get("caption_end", timing.get("end"))
        scene_end = timing.get("end", end)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Caption scene text cannot be empty")
        if (isinstance(start, bool) or isinstance(end, bool)
                or isinstance(scene_end, bool)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not isinstance(scene_end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)
                or not math.isfinite(scene_end)
                or start < 0 or end <= start or end > scene_end + 0.01):
            raise ValueError(
                "Caption timestamps must be finite, ordered, and stay inside their scene"
            )
        lines = _wrap_caption_text(text.strip(), max_line_width, font_size)
        pages = ["\n".join(lines[i:i + 2]) for i in range(0, len(lines), 2)]
        page_duration = (end - start) / len(pages)
        if len(pages) > 1 and page_duration < 0.7:
            raise ValueError(
                f"Scene {index} caption is too long to display legibly in "
                f"{end - start:.2f} seconds; increase its duration or shorten its text"
            )
        for page_index, page in enumerate(pages):
            page_start = start + page_index * page_duration
            page_end = end if page_index == len(pages) - 1 else start + (page_index + 1) * page_duration
            events.append(
                f"Dialogue: 0,{_ts(page_start)},{_ts(page_end)},Scene,,0,0,0,,"
                f"{_escape_ass_text(page)}"
            )

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Scene,DejaVu Sans,{font_size},&H00FFFFFF,&H000000FF,&H99000000,&H50000000,1,0,0,0,100,100,0,0,1,{outline},{shadow},{alignment},{margin_x},{margin_x},{margin_y},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    output_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return output_path


def build_captions(cfg: dict, voice_path: Path, out_dir: Path,
                   hook_title: str = "") -> Optional[Path]:
    if not cfg["captions"].get("enabled", True):
        return None
    from faster_whisper import WhisperModel

    style = cfg["captions"]["style"]
    model = WhisperModel(cfg["captions"].get("model", "base"), compute_type="int8")
    segments, _ = model.transcribe(str(voice_path), word_timestamps=True)

    # Flatten to words with timings.
    words = []
    for seg in segments:
        for w in (seg.words or []):
            token = w.word.strip()
            if token:
                words.append((token, w.start, w.end))
    if not words:
        log("captions: no words detected; skipping", "warn")
        return None

    max_words = style.get("max_words_per_cue", 3)
    uppercase = style.get("uppercase", True)
    profile = get_output_profile(cfg["video"].get("aspect_ratio", "9:16"))
    res_x, res_y = profile.width, profile.height
    fontname = style.get("fontname", "Montserrat")
    requested_font_size = int(style["font_size"])
    font_size_limit = max(1, round(min(res_x * 0.08, res_y * 0.055)))
    font_size = max(1, min(requested_font_size, font_size_limit))
    outline = max(1, round(font_size * 0.04))
    shadow = max(1, round(font_size * 0.02))
    margin_x = round(res_x * 0.09)
    max_line_width = res_x - 2 * margin_x - 2 * (outline + shadow)
    align = {"top": 8, "middle": 5, "bottom": 2}.get(style.get("position", "middle"), 5)
    primary = style["primary_color"]
    highlight = style.get("highlight_color", "&H0000F2FF")
    outline_col = style.get("outline_color", "&H00000000")
    safe_zone_padding = round(
        cfg["video"].get("safe_zone_padding", 220) * res_y / 1920
    )
    hook_margin = round(300 * res_y / 1920)

    # Keep outline and margins inside a conservative 9% horizontal safe area.
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {res_x}
PlayResY: {res_y}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Base,{fontname},{font_size},{primary},{outline_col},&H96000000,1,{outline},{shadow},{align},{margin_x},{margin_x},{safe_zone_padding},1
Style: Hook,{fontname},{int(font_size * 0.82)},{highlight},{outline_col},&H96000000,1,{outline},{shadow},8,{margin_x},{margin_x},{hook_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    # Animated hook title flashed top-of-frame for the first ~2.6s (pattern-interrupt).
    title_events = ""
    hook_dur = float(cfg["captions"].get("hook_title_seconds", 2.6))
    hook_title = _clean(hook_title)
    if hook_title:
        disp = hook_title.upper() if uppercase else hook_title
        disp = disp.replace("\n", " ").strip()
        hook_font_size = max(1, int(font_size * 0.82))
        hook_lines = _wrap_caption_text(
            disp, max_line_width, hook_font_size, fontname,
        )
        hook_text = r"\N".join(_escape_ass_text(line) for line in hook_lines)
        title_events = (
            f"Dialogue: 0,{_ts(0.1)},{_ts(hook_dur)},Hook,,0,0,0,,"
            f"{{\\fad(150,250)\\fscx84\\fscy84\\t(0,220,\\fscx100\\fscy100)}}{hook_text}\n"
        )

    def animate_active(disp: str) -> str:
        # Scale "pop": 88% → 107% → 100% in the first ~170ms, bold + highlight color.
        return (
            f"{{\\b1\\1c{highlight}\\fscx88\\fscy88"
            f"\\t(0,90,\\fscx107\\fscy107)\\t(90,170,\\fscx100\\fscy100)}}"
            f"{disp}{{\\r}}"
        )

    lines = []
    # Group words into small cues; within a cue, the spoken word pops + highlights.
    for i in range(0, len(words), max_words):
        group = words[i : i + max_words]
        g_start, g_end = group[0][1], group[-1][2]
        for j, (tok, w_start, w_end) in enumerate(group):
            display_words = [
                (t2.upper() if uppercase else t2).split()
                for t2, _, _ in group
            ]
            flattened_words = [
                (word, source_index)
                for source_index, tokens in enumerate(display_words)
                for word in tokens
            ]
            plain_text = " ".join(word for word, _ in flattened_words)
            wrapped_lines = _wrap_caption_text(
                plain_text, max_line_width, font_size, fontname,
            )
            wrapped_text = []
            word_index = 0
            word_offset = 0
            for wrapped_line in wrapped_lines:
                wrapped_words = []
                for piece in wrapped_line.split():
                    while (word_index < len(flattened_words)
                           and word_offset == len(flattened_words[word_index][0])):
                        word_index += 1
                        word_offset = 0
                    if word_index >= len(flattened_words):
                        raise RuntimeError("Caption wrapping produced unexpected text")
                    source_word, source_index = flattened_words[word_index]
                    if not source_word[word_offset:].startswith(piece):
                        raise RuntimeError("Caption wrapping changed narration text")
                    escaped_piece = _escape_ass_text(piece)
                    wrapped_words.append(
                        animate_active(escaped_piece)
                        if source_index == j else escaped_piece
                    )
                    word_offset += len(piece)
                wrapped_text.append(" ".join(wrapped_words))
            text = r"\N".join(wrapped_text)
            # Gapless within the cue: hold each word until the next word begins.
            start = w_start if j > 0 else g_start
            end = group[j + 1][1] if j < len(group) - 1 else g_end
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Base,,0,0,0,,{text}")

    ass_path = out_dir / "captions.ass"
    ass_path.write_text(header + title_events + "\n".join(lines) + "\n", encoding="utf-8")
    log(f"captions: captions.ass ({len(words)} words, animated)", "ok")
    return ass_path
