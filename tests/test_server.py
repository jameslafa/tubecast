import json
import threading
import urllib.error
import urllib.request

import pytest

from tubecast.config import parse
from tubecast.server import App, make_server, parse_range


@pytest.mark.parametrize("header, size, expected", [
    (None, 100, None),
    ("bytes=0-", 100, (0, 99)),
    ("bytes=10-19", 100, (10, 19)),
    ("bytes=90-500", 100, (90, 99)),
    ("bytes=-10", 100, (90, 99)),
    ("bytes=-500", 100, (0, 99)),
    ("bytes=0-1,5-6", 100, None),  # several ranges: whole file
    ("items=0-1", 100, None),
    ("bytes=-", 100, None),
])
def test_parse_range(header, size, expected):
    assert parse_range(header, size) == expected


@pytest.mark.parametrize("header", ["bytes=100-", "bytes=20-10", "bytes=-0"])
def test_parse_range_unsatisfiable(header):
    with pytest.raises(ValueError):
        parse_range(header, 100)


AUDIO = bytes(range(256)) * 4  # 1024 bytes


@pytest.fixture
def base(tmp_path):
    (tmp_path / "audio" / "test").mkdir(parents=True)
    (tmp_path / "audio" / "test" / "abc_-1.m4a").write_bytes(AUDIO)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "test.json").write_text(json.dumps({
        "channel": {"title": "Test"},
        "episodes": {"abc_-1": {"title": "Ep", "description": "", "timestamp": 1_800_000_000,
                                "duration": 60, "thumbnail": None, "file": "abc_-1.m4a", "size": 1024}}}))
    app = App(tmp_path)
    app.config = parse({"base_url": "https://tube.example", "channels": ["@Test"]})
    server = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def get(url, method="GET", **headers):
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_whole_file(base):
    status, headers, body = get(f"{base}/audio/test/abc_-1.m4a")
    assert status == 200
    assert body == AUDIO
    assert headers["Content-Type"] == "audio/mp4"
    assert headers["Accept-Ranges"] == "bytes"


def test_range(base):
    status, headers, body = get(f"{base}/audio/test/abc_-1.m4a", Range="bytes=1000-")
    assert status == 206
    assert body == AUDIO[1000:]
    assert headers["Content-Range"] == "bytes 1000-1023/1024"
    assert headers["Content-Length"] == "24"


def test_range_unsatisfiable(base):
    status, headers, _ = get(f"{base}/audio/test/abc_-1.m4a", Range="bytes=5000-")
    assert status == 416
    assert headers["Content-Range"] == "bytes */1024"


def test_head(base):
    status, headers, body = get(f"{base}/audio/test/abc_-1.m4a", method="HEAD")
    assert (status, headers["Content-Length"], body) == (200, "1024", b"")


def test_feed(base):
    status, headers, body = get(f"{base}/feeds/test.xml")
    assert status == 200
    assert headers["Content-Type"].startswith("application/rss+xml")
    assert b"https://tube.example/audio/test/abc_-1.m4a" in body


def test_index(base):
    status, _, body = get(f"{base}/")
    assert status == 200
    assert b"https://tube.example/feeds/test.xml" in body


@pytest.mark.parametrize("path", [
    "/feeds/other.xml", "/audio/other/abc_-1.m4a", "/audio/test/missing.m4a",
    "/audio/test/../../state/test.json", "/audio/test/%2e%2e/x.m4a", "/nope",
])
def test_not_found(base, path):
    assert get(f"{base}{path}")[0] == 404
