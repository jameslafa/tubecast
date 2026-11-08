"""HTTP server for the feeds and audio files, with the Range requests podcast apps seek with."""

import html
import logging
import re
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import feed
from .config import Config
from .sync import audio_dir, load_state

log = logging.getLogger(__name__)

CHUNK = 256 * 1024
FEED_PATH = re.compile(r"/feeds/([a-z0-9][a-z0-9_-]*)\.xml")
AUDIO_PATH = re.compile(r"/audio/([a-z0-9][a-z0-9_-]*)/([\w-]+\.m4a)")


class App:
    """What the server reads: the data folder and the config the scheduler loaded last."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.config: Config | None = None
        self.config_error: str | None = None


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
        config = self.app.config
        if path == "/":
            return self._send(HTTPStatus.OK, self._index(config), "text/html; charset=utf-8", head)
        channels = {c.slug: c for c in config.channels} if config else {}
        if match := FEED_PATH.fullmatch(path):
            channel = channels.get(match.group(1))
            if channel:
                state = load_state(self.app.data_dir, channel.slug)
                body = feed.build(channel, state, config.base_url)
                return self._send(HTTPStatus.OK, body, "application/rss+xml; charset=utf-8", head)
        if match := AUDIO_PATH.fullmatch(path):
            slug, name = match.groups()
            if slug in channels:
                return self._file(audio_dir(self.app.data_dir, slug) / name, "audio/mp4", head)
        self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain", head)

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
        for channel in config.channels if config else ():
            state = load_state(self.app.data_dir, channel.slug)
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
        log.debug("%s %s", self.address_string(), format % args)


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
