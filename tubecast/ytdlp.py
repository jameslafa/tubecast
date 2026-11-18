"""Runs yt-dlp as a subprocess, so a yt-dlp update takes effect without restarting tubecast."""

import json
import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

LIST_TIMEOUT = 300
DOWNLOAD_TIMEOUT = 3 * 3600
# YouTube's own AAC audio when there is one; anything else is converted to m4a. Sites without an
# audio-only stream (X, Instagram) get their video, the audio taken out
FORMAT = "bestaudio[ext=m4a]/bestaudio/best"
# When YouTube refuses the https streams with a 403 (seen on a video out for 15 hours, 2026-11-18),
# its HLS streams still work. yt-dlp's own order still puts the original language before dubs
HLS_FORMAT = "bestaudio[protocol^=m3u8]/best[protocol^=m3u8]"


class YtDlpError(Exception):
    pass


class YtDlp:
    def __init__(self, data_dir: Path):
        self.cache_dir = data_dir / "cache" / "yt-dlp"
        self.cookies = data_dir / "cookies.txt"

    def _run(self, args: list[str], timeout: int) -> str:
        cmd = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-progress",
               "--cache-dir", str(self.cache_dir)]
        if self.cookies.exists():
            cmd += ["--cookies", str(self.cookies)]
        try:
            result = subprocess.run(cmd + args, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise YtDlpError(f"timed out after {timeout}s") from None
        if result.returncode != 0:
            errors = [line for line in result.stderr.splitlines() if line.startswith("ERROR")]
            raise YtDlpError((errors or result.stderr.strip().splitlines() or ["failed"])[-1])
        return result.stdout

    def version(self) -> str:
        return self._run(["--version"], 60).strip()

    def update(self) -> None:
        """Updates yt-dlp (and its YouTube helper, yt-dlp-ejs) to the latest stable release."""
        before = self.version()
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--no-cache-dir",
             "--disable-pip-version-check", "--upgrade", "yt-dlp[default]"],
            capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise YtDlpError(result.stderr.strip().splitlines()[-1] if result.stderr else "pip failed")
        after = self.version()
        if after != before:
            log.info("updated yt-dlp %s -> %s", before, after)
        else:
            log.info("yt-dlp %s is up to date", after)

    def list_tab(self, url: str, count: int) -> dict | None:
        """The channel tab's info with its newest `count` entries, None if the tab doesn't exist."""
        try:
            out = self._run(["-J", "--flat-playlist", "--playlist-end", str(count), url], LIST_TIMEOUT)
        except YtDlpError as e:
            if "does not have a" in str(e):  # e.g. "This channel does not have a shorts tab"
                return None
            raise
        return json.loads(out)

    def video_info(self, video_id: str) -> dict:
        return self.url_info(watch_url(video_id))

    def url_info(self, url: str) -> dict:
        """The info of one video, on any site yt-dlp handles."""
        out = self._run(["-J", "--no-playlist", url], LIST_TIMEOUT)
        return json.loads(out)

    def download(self, url: str, dest_dir: Path, sponsorblock: tuple[str, ...],
                 name: str = "%(id)s") -> tuple[Path, list[tuple[float, float]]]:
        """Downloads the audio as m4a to dest_dir/<name>.m4a, sponsor segments cut out. Returns
        the file and the segments cut, as (start, end) seconds of the original video. yt-dlp
        cuts segments only on YouTube, the one site SponsorBlock covers."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "--no-playlist",
            "-x", "--audio-format", "m4a",
            "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg",
            "-o", str(dest_dir / f"{name}.%(ext)s"),
            "--print", "after_move:%(.{filepath,sponsorblock_chapters})j",
        ]
        if sponsorblock:
            args += ["--sponsorblock-remove", ",".join(sponsorblock)]
        try:
            out = self._run(args + ["-f", FORMAT, url], DOWNLOAD_TIMEOUT)
        except YtDlpError as e:
            if "403" not in str(e):
                raise
            log.info("%s: HTTP 403, trying the HLS stream", url)
            out = self._run(args + ["-f", HLS_FORMAT, url], DOWNLOAD_TIMEOUT)
        try:
            result = json.loads(out.strip().splitlines()[-1])
            path = Path(result["filepath"])
        except (IndexError, ValueError, KeyError, TypeError):
            raise YtDlpError("yt-dlp did not report the downloaded file") from None
        segments = result.get("sponsorblock_chapters") or []
        return path, [(s["start_time"], s["end_time"]) for s in segments]


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def file_chapters(path: Path) -> list[dict]:
    """The chapters embedded in an audio file, as [{"start": seconds, "title": ...}]."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60)
        chapters = json.loads(result.stdout).get("chapters") or []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return [{"start": float(c["start_time"]), "title": (c.get("tags") or {}).get("title") or ""}
            for c in chapters]


def audio_duration(path: Path) -> int | None:
    """The real duration of an audio file in seconds, which is shorter after sponsor cuts."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60)
        return round(float(result.stdout.strip()))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
