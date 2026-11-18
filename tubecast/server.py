"""HTTP server for the feeds and audio files, with the Range requests podcast apps seek with."""

import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import __version__, feed, later
from .chapters import to_json
from .config import LATER_SLUG, Channel, Config
from .sync import audio_dir, load_state

log = logging.getLogger(__name__)

CHUNK = 256 * 1024
FEED_PATH = re.compile(r"/feeds/([a-z0-9][a-z0-9_-]*)\.xml")
AUDIO_PATH = re.compile(r"/audio/([a-z0-9][a-z0-9_-]*)/([\w-]+\.m4a)")
CHAPTERS_PATH = re.compile(r"/chapters/([a-z0-9][a-z0-9_-]*)/([\w-]+)\.json")
MAX_BODY = 10_000  # bytes a POST /later may send


class App:
    """What the server reads: the data folder and the config the scheduler loaded last."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.config: Config | None = None
        self.config_error: str | None = None
        self.ytdlp_version: str | None = None
        self.last_check: float | None = None  # when the last check of all channels finished
        self.later: later.Later | None = None  # takes the URLs sent to POST /later

    def stats(self) -> dict:
        """Counts for dashboards such as Homepage, and the versions for update checks."""
        channels = self.config.channels if self.config else ()
        episodes = sum(len(load_state(self.data_dir, c.slug)["episodes"]) for c in channels)
        last_check = last_check_ago = None
        if self.last_check:
            last_check = datetime.fromtimestamp(self.last_check, timezone.utc).isoformat()
            last_check_ago = ago(time.time() - self.last_check)
        # What the audio files take on disk, leftovers of removed channels included
        disk_bytes = sum(p.stat().st_size for p in (self.data_dir / "audio").rglob("*") if p.is_file())
        later_episodes = None
        if self.config and self.config.later:
            later_episodes = len(later.load(self.data_dir)["episodes"])
        return {"version": __version__, "yt_dlp": self.ytdlp_version, "channels": len(channels),
                "episodes": episodes, "later_episodes": later_episodes, "disk_bytes": disk_bytes,
                "last_check": last_check, "last_check_ago": last_check_ago,
                "config_error": self.config_error}

    def feeds(self) -> dict[str, Channel]:
        """The feeds served, by slug: Listen later (when on) and the channels."""
        config = self.config
        if not config:
            return {}
        feeds = {LATER_SLUG: later.channel(config)} if config.later else {}
        return feeds | {c.slug: c for c in config.channels}

    def state(self, slug: str) -> dict:
        return later.load(self.data_dir) if slug == LATER_SLUG else load_state(self.data_dir, slug)


def ago(seconds: float) -> str:
    """A short "how long ago" for dashboards: "now", "2 min ago", "3h ago", "2d ago"."""
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The (start, end) bytes, end included, of a single "bytes=" range.

    None means serve the whole file: no header, or one we don't handle (several ranges),
    which the HTTP spec lets a server ignore. Raises ValueError if the range is outside the file.
    """
    if not header:
        return None
    match = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header)
    if not match or match.group(1) == match.group(2) == "":
        return None
    first, last = match.groups()
    if first == "":  # "bytes=-500": the last 500 bytes
        if int(last) == 0 or size == 0:
            raise ValueError("unsatisfiable range")
        return max(size - int(last), 0), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or (last and int(last) < start):
        raise ValueError("unsatisfiable range")
    return start, end


class Handler(BaseHTTPRequestHandler):
    server_version = "tubecast"
    app: App  # set by make_server

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head: bool = False):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self._send(HTTPStatus.OK, b"ok\n", "text/plain", head)
        if path == "/stats":
            body = json.dumps(self.app.stats()).encode()
            return self._send(HTTPStatus.OK, body, "application/json", head)
        config = self.app.config
        if path == "/":
            return self._send(HTTPStatus.OK, self._index(config), "text/html; charset=utf-8", head)
        channels = self.app.feeds()
        if match := FEED_PATH.fullmatch(path):
            channel = channels.get(match.group(1))
            if channel:
                state = self.app.state(channel.slug)
                body = feed.build(channel, state, config.base_url)
                return self._send(HTTPStatus.OK, body, "application/rss+xml; charset=utf-8", head)
        if match := CHAPTERS_PATH.fullmatch(path):
            slug, vid = match.groups()
            if slug in channels:
                episode = self.app.state(slug)["episodes"].get(vid)
                if episode and episode.get("chapters"):
                    body = json.dumps(to_json(episode["chapters"]), ensure_ascii=False).encode()
                    return self._send(HTTPStatus.OK, body, "application/json+chapters", head)
        if match := AUDIO_PATH.fullmatch(path):
            slug, name = match.groups()
            if slug in channels:
                return self._file(audio_dir(self.app.data_dir, slug) / name, "audio/mp4", head)
        self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain", head)

    def do_POST(self):
        """POST /later with a video URL, as JSON ({"url": ...}), a form or plain text."""
        path = self.path.split("?", 1)[0]
        config = self.app.config
        if path != "/later" or not (config and config.later and self.app.later):
            return self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain", False)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"too long\n", "text/plain", False)
        text = self.rfile.read(length).decode("utf-8", "replace")
        if "json" in (self.headers.get("Content-Type") or ""):
            try:
                text = str(json.loads(text).get("url") or "")
            except (ValueError, AttributeError):
                text = ""
        elif "form-urlencoded" in (self.headers.get("Content-Type") or ""):
            # A form's url field, or plain text sent as a form (curl -d does)
            text = (parse_qs(text).get("url") or [text])[0]
        url = later.find_url(text)
        if not url:
            body = json.dumps({"error": "expected a video URL"}).encode()
            return self._send(HTTPStatus.BAD_REQUEST, body, "application/json", False)
        self.app.later.add(url)
        body = json.dumps({"queued": url}).encode()
        self._send(HTTPStatus.ACCEPTED, body, "application/json", False)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str, head: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _file(self, path: Path, content_type: str, head: bool) -> None:
        try:
            f = path.open("rb")
        except (FileNotFoundError, IsADirectoryError):
            return self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain", head)
        with f:
            stat = path.stat()
            size = stat.st_size
            try:
                byte_range = parse_range(self.headers.get("Range"), size)
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = byte_range or (0, size - 1)
            length = end - start + 1 if size else 0
            self.send_response(HTTPStatus.PARTIAL_CONTENT if byte_range else HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", formatdate(stat.st_mtime, usegmt=True))
            if byte_range:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head:
                return
            f.seek(start)
            try:
                remaining = length
                while remaining:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # podcast apps often drop a connection to seek elsewhere

    def _index(self, config: Config | None) -> bytes:
        rows = []
        for channel in self.app.feeds().values():
            state = self.app.state(channel.slug)
            title = (state["channel"] or {}).get("title") or channel.slug
            url = feed.feed_url(config.base_url, channel.slug)
            rows.append(f"<li><b>{html.escape(title)}</b> ({len(state['episodes'])} episodes)<br>"
                        f'<a href="{html.escape(url)}">{html.escape(url)}</a></li>')
        if self.app.config_error:
            rows.insert(0, f'<li class="error">Config error: {html.escape(self.app.config_error)}</li>')
        if not rows:
            rows.append("<li>No channels yet: add some to config.yaml.</li>")
        page = INDEX.replace("{rows}", "\n".join(rows))
        return page.encode()

    def log_message(self, format, *args):
        # The client and range help see what a podcast app asks for
        log.debug("%s %s range=%s ua=%r", self.address_string(), format % args,
                  self.headers.get("Range") if self.headers else None,
                  self.headers.get("User-Agent") if self.headers else None)


INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tubecast</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.5 system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem; }
  li { margin-bottom: 1rem; overflow-wrap: anywhere; }
  .error { color: #c62828; }
</style></head>
<body><h1>tubecast</h1>
<p>Add a feed URL to your podcast app.</p>
<ul>
{rows}
</ul></body></html>
"""


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
