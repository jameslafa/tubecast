import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tubecast import later
from tubecast.config import ConfigError, parse, parse_size
from tubecast.feed import build
from tubecast.later import Later, episode_id, find_url, format_start, parse_start, to_delete
from tubecast.server import App, make_server
from tubecast.ytdlp import YtDlpError

DAY = 24 * 3600
NOW = 1_800_000_000
CONFIG = parse({"base_url": "https://tube.example", "later": {"keep_days": 14, "max_size": "1KB"}})


@pytest.mark.parametrize("url, seconds", [
    ("https://youtu.be/abc?t=754", 754),
    ("https://www.youtube.com/watch?v=abc&t=754s", 754),
    ("https://www.youtube.com/watch?v=abc&t=12m34s", 754),
    ("https://www.youtube.com/watch?v=abc&t=1h2m3s", 3723),
    ("https://vimeo.com/123#t=90", 90),
    ("https://www.youtube.com/embed/abc?start=30", 30),
    ("https://youtu.be/abc", None),
    ("https://youtu.be/abc?t=0", None),
    ("https://youtu.be/abc?t=soon", None),
])
def test_parse_start(url, seconds):
    assert parse_start(url) == seconds


def test_format_start():
    assert format_start(754) == "12:34"
    assert format_start(5) == "0:05"
    assert format_start(3723) == "1:02:03"


def test_find_url():
    assert find_url("Great talk https://youtu.be/abc?t=5 via app") == "https://youtu.be/abc?t=5"
    assert find_url("  https://x.com/a/status/1\n") == "https://x.com/a/status/1"
    assert find_url("no link here") is None


def test_short():
    assert later.short("ERROR: [vimeo] 769: The web client only works when logged-in. Use --cookies") == (
        "The web client only works when logged-in")
    assert later.short("timed out after 300s") == "timed out after 300s"


def test_episode_id():
    assert episode_id({"id": "abc_-1", "extractor_key": "Youtube"}) == "abc_-1"
    assert episode_id({"id": "1859", "extractor_key": "Twitter"}) == "twitter-1859"
    assert episode_id({"id": "a.b/c"}) == "video-a_b_c"


def test_to_delete_by_age_then_size():
    episodes = {
        "old": {"timestamp": NOW - 15 * DAY, "size": 100},
        "mid": {"timestamp": NOW - 3 * DAY, "size": 600},
        "new": {"timestamp": NOW - DAY, "size": 600},
    }
    assert to_delete(episodes, NOW, 14, 10_000) == ["old"]
    assert to_delete(episodes, NOW, 14, 1000) == ["old", "mid"]
    # The newest stays, even too old or too big
    assert to_delete({"only": {"timestamp": NOW - 30 * DAY, "size": 5000}}, NOW, 14, 1000) == []


def test_config():
    assert parse({"base_url": "http://x"}).later is None
    on = parse({"base_url": "http://x", "later": True}).later
    assert (on.keep_days, on.max_size) == (14, 5_000_000_000)
    assert CONFIG.later.max_size == 1000
    assert parse_size("1.5GB", "k") == 1_500_000_000
    assert parse_size(2048, "k") == 2048
    with pytest.raises(ConfigError, match="later: unknown"):
        parse({"base_url": "http://x", "later": {"token": "x"}})
    with pytest.raises(ConfigError, match="size"):
        parse({"base_url": "http://x", "later": {"max_size": "lots"}})
    with pytest.raises(ConfigError, match="Listen later"):
        parse({"base_url": "http://x", "channels": [{"url": "@Test", "slug": "later"}]})


class FakeYtDlp:
    def __init__(self, fail_times=0):
        self.downloads = []
        self.fail_times = fail_times

    def url_info(self, url):
        if self.fail_times:
            self.fail_times -= 1
            raise YtDlpError("Video unavailable")
        return {"id": "abc", "extractor_key": "Youtube", "title": "A talk", "channel": "Some Channel",
                "description": "00:00 Intro\n05:00 Main", "timestamp": NOW - 30 * DAY, "duration": 600,
                "webpage_url": "https://www.youtube.com/watch?v=abc"}

    def download(self, url, dest_dir: Path, sponsorblock, name):
        self.downloads.append(url)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{name}.m4a"
        path.write_bytes(b"audio" * 10)
        return path, [(60, 120)]


@pytest.fixture
def files(monkeypatch):
    monkeypatch.setattr("tubecast.sync.audio_duration", lambda path: 540)
    monkeypatch.setattr("tubecast.sync.file_chapters", lambda path: [])


def test_process_downloads_then_moves_a_sent_again_video_to_the_top(tmp_path, files):
    yt = FakeYtDlp()
    queue = Later(tmp_path, yt, lambda: CONFIG, retry_after=0)
    queue.process(CONFIG, "https://youtu.be/abc?t=754", NOW)
    state = later.load(tmp_path)
    episode = state["episodes"]["abc"]
    assert episode["timestamp"] == NOW
    assert episode["uploaded"] == NOW - 30 * DAY
    assert episode["start"] == 754
    assert episode["description"] == "00:00 Intro\n04:00 Main"
    assert episode["link"] == "https://www.youtube.com/watch?v=abc"

    queue.process(CONFIG, "https://youtu.be/abc", NOW + DAY)
    episode = later.load(tmp_path)["episodes"]["abc"]
    assert len(yt.downloads) == 1  # not downloaded again
    assert (episode["timestamp"], episode["start"]) == (NOW + DAY, None)


def test_add_keeps_the_url_until_processed(tmp_path):
    queue = Later(tmp_path, FakeYtDlp(), lambda: CONFIG)
    queue.add("https://youtu.be/abc")
    queue.add("https://youtu.be/abc")
    assert [p["url"] for p in later.load(tmp_path)["pending"]] == ["https://youtu.be/abc"]


def test_failure_is_retried_then_pushed(tmp_path, files, monkeypatch):
    sent = []
    monkeypatch.setattr(later, "notify", lambda url, message: sent.append((url, message)))
    queue = Later(tmp_path, FakeYtDlp(fail_times=1), lambda: CONFIG, "https://ntfy.example/t", retry_after=0)
    queue.add("https://youtu.be/abc")
    queue.process(CONFIG, "https://youtu.be/abc", NOW)
    assert sent == []
    assert list(later.load(tmp_path)["episodes"]) == ["abc"]
    assert later.load(tmp_path)["pending"] == []

    queue = Later(tmp_path, FakeYtDlp(fail_times=2), lambda: CONFIG, "https://ntfy.example/t", retry_after=0)
    queue.add("https://youtu.be/gone")
    queue.process(CONFIG, "https://youtu.be/gone", NOW)
    assert sent == [("https://ntfy.example/t", "https://youtu.be/gone\nVideo unavailable")]
    assert later.load(tmp_path)["pending"] == []


def test_feed(tmp_path, files):
    Later(tmp_path, FakeYtDlp(), lambda: CONFIG).process(CONFIG, "https://youtu.be/abc?t=754", NOW)
    rss = ET.fromstring(build(later.channel(CONFIG), later.load(tmp_path), CONFIG.base_url))
    channel = rss.find("channel")
    assert channel.findtext("title") == "Listen later"
    item = channel.find("item")
    assert item.findtext("title") == "A talk (from 12:34)"
    assert item.findtext("guid") == "abc"
    assert item.findtext("description").startswith("By Some Channel, 16 Dec 2026\n\n00:00 Intro")
    assert item.find("enclosure").get("url") == "https://tube.example/audio/later/abc.m4a"


@pytest.fixture
def server(tmp_path):
    app = App(tmp_path)
    app.config = CONFIG
    app.later = Later(tmp_path, FakeYtDlp(), lambda: CONFIG)
    httpd = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def post(url, body: bytes, content_type):
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_post_later(server, tmp_path):
    app, base = server
    assert post(f"{base}/later", b'{"url": "https://youtu.be/a"}', "application/json") == (
        202, {"queued": "https://youtu.be/a"})
    assert post(f"{base}/later", b"Look https://youtu.be/b", "text/plain")[0] == 202
    assert post(f"{base}/later", b"url=https%3A%2F%2Fyoutu.be%2Fc", "application/x-www-form-urlencoded")[0] == 202
    assert post(f"{base}/later", b"https://youtu.be/d", "application/x-www-form-urlencoded")[0] == 202  # curl -d
    assert [p["url"] for p in later.load(tmp_path)["pending"]] == [
        "https://youtu.be/a", "https://youtu.be/b", "https://youtu.be/c", "https://youtu.be/d"]
    assert post(f"{base}/later", b"nothing", "text/plain")[0] == 400
    assert post(f"{base}/other", b"https://youtu.be/a", "text/plain")[0] == 404
    with urllib.request.urlopen(f"{base}/feeds/later.xml") as response:
        assert b"<title>Listen later</title>" in response.read()
    with urllib.request.urlopen(f"{base}/stats") as response:
        assert json.loads(response.read())["later_episodes"] == 0


def test_post_later_when_off(server):
    app, base = server
    app.config = parse({"base_url": "https://tube.example"})
    assert post(f"{base}/later", b"https://youtu.be/a", "text/plain")[0] == 404


def test_failed_download_leaves_no_files(tmp_path, files, monkeypatch):
    monkeypatch.setattr(later, "notify", lambda url, message: None)
    yt = FakeYtDlp()

    def fail(url, dest_dir, sponsorblock, name):
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / f"{name}.jpg").write_bytes(b"x")
        raise YtDlpError("HTTP Error 403: Forbidden")
    yt.download = fail
    Later(tmp_path, yt, lambda: CONFIG, retry_after=0).process(CONFIG, "https://youtu.be/abc", NOW)
    assert list((tmp_path / "audio" / "later").iterdir()) == []
