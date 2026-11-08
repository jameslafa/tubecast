"""Chapters and timestamps after SponsorBlock cuts.

Cutting segments out of the audio makes every later moment come earlier, so the timestamps in a
video's description no longer match. These helpers move them to the cut audio's timeline.
"""

import re

Cuts = list[tuple[float, float]]  # removed (start, end) seconds on the original timeline


def merge(segments: list[tuple[float, float]]) -> Cuts:
    """Sorted, non-overlapping cuts from SponsorBlock segments, which may overlap."""
    cuts: Cuts = []
    for start, end in sorted(segments):
        if cuts and start <= cuts[-1][1]:
            cuts[-1] = (cuts[-1][0], max(cuts[-1][1], end))
        elif end > start:
            cuts.append((start, end))
    return cuts


def after_cuts(t: float, cuts: Cuts) -> float:
    """Where moment t of the original lands in the cut audio. A moment inside a cut lands where
    the cut was, which is where what came after it now starts."""
    removed = 0.0
    for start, end in cuts:
        if t <= start:
            break
        removed += min(t, end) - start
    return t - removed


TIMESTAMP = re.compile(r"^(\W*)((?:\d{1,2}:)?\d{1,2}:\d{2})\b", re.MULTILINE)


def _seconds(stamp: str) -> int:
    seconds = 0
    for part in stamp.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


def _format(seconds: float, like: str) -> str:
    """Seconds as a timestamp in the same style as `like` (h:mm:ss or mm:ss, padding kept)."""
    total = int(seconds)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    parts = like.split(":")
    if len(parts) == 3 or h:
        return f"{h:0{len(parts[0]) if len(parts) == 3 else 1}d}:{m:02d}:{s:02d}"
    return f"{m:0{len(parts[0])}d}:{s:02d}"


def shift_timestamps(text: str, cuts: Cuts) -> str:
    """Moves the timestamps that start a line (a chapter list) to the cut audio's timeline.
    Times elsewhere in the text, like "doors open at 10:30", are left alone."""
    if not cuts:
        return text
    return TIMESTAMP.sub(
        lambda m: m.group(1) + _format(after_cuts(_seconds(m.group(2)), cuts), m.group(2)), text)


def to_json(chapters: list[dict]) -> dict:
    """A Podcasting 2.0 chapters file (https://github.com/Podcastindex-org/podcast-namespace/
    blob/main/docs/examples/chapters/jsonChapters.md) from [{"start": seconds, "title": ...}]."""
    return {"version": "1.2.0",
            "chapters": [{"startTime": round(c["start"], 3), "title": c["title"]} for c in chapters]}
