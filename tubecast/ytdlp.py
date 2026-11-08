"""Runs yt-dlp as a subprocess, so a yt-dlp update takes effect without restarting tubecast."""

import json
import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

LIST_TIMEOUT = 300
DOWNLOAD_TIMEOUT = 3 * 3600


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
        out = self._run(["-J", "--no-playlist", watch_url(video_id)], LIST_TIMEOUT)
        return json.loads(out)

    def download(self, video_id: str, dest_dir: Path, sponsorblock: tuple[str, ...]) -> Path:
        """Downloads the audio as m4a to dest_dir/<id>.m4a, sponsor segments cut out."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "--no-playlist",
            # YouTube's own AAC audio when there is one; anything else is converted to m4a
            "-f", "bestaudio[ext=m4a]/bestaudio",
            "-x", "--audio-format", "m4a",
            "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg",
            "-o", str(dest_dir / "%(id)s.%(ext)s"),
            "--print", "after_move:filepath",
        ]
        if sponsorblock:
            args += ["--sponsorblock-remove", ",".join(sponsorblock)]
        out = self._run(args + [watch_url(video_id)], DOWNLOAD_TIMEOUT)
        lines = out.strip().splitlines()
        if not lines:
            raise YtDlpError("yt-dlp did not report the downloaded file")
        return Path(lines[-1])


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def audio_duration(path: Path) -> int | None:
    """The real duration of an audio file in seconds, which is shorter after sponsor cuts."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60)
        return round(float(result.stdout.strip()))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
