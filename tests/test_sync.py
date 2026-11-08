from pathlib import Path

import pytest

from tubecast.config import Channel, parse
from tubecast.sync import load_state, sync_channel, tabs, to_delete, to_download
from tubecast.ytdlp import YtDlpError

HOUR = 3600
NOW = 1_800_000_000


def channel(**options) -> Channel:
    defaults = dict(url="https://www.youtube.com/@Test", slug="test", keep=3, min_duration=0,
                    include_shorts=False, include_live=False)
    return Channel(**{**defaults, **options})


def video(age_hours: float, **extra) -> dict:
    return {"title": "t", "description": "", "timestamp": NOW - age_hours * HOUR,
            "duration": 1200, "live_status": "not_live", "thumbnail": None, **extra}


def episode(age_hours: float) -> dict:
    return {"title": "t", "description": "", "timestamp": NOW - age_hours * HOUR,
            "duration": 1200, "thumbnail": None, "file": "x.m4a", "size": 1}


def test_backfill_downloads_newest_keep_old_enough():
    videos = {"new": video(1), "a": video(10), "b": video(20), "c": video(30), "d": video(40)}
    assert to_download({}, videos, channel(), NOW, 6 * HOUR) == ["a", "b", "c"]


def test_only_newer_than_existing_episodes():
    episodes = {"a": episode(10), "b": episode(20), "c": episode(30)}
    videos = {"n": video(7), "a": video(10), "b": video(20), "c": video(30), "d": video(40)}
    assert to_download(episodes, videos, channel(), NOW, 6 * HOUR) == ["n"]


def test_never_downloads_what_would_be_deleted():
    # "b" was taken down on YouTube: "d" is now listed among the newest, but older than all episodes
    episodes = {"a": episode(10), "b": episode(20), "c": episode(30)}
    videos = {"a": video(10), "c": video(30), "d": video(40)}
    assert to_download(episodes, videos, channel(), NOW, 6 * HOUR) == []


def test_filters():
    videos = {
        "short": video(10, duration=50),
        "premiere": video(11, live_status="was_live"),
        "failed": {"error": "members only", "checked": NOW},
        "undated": video(12, timestamp=None),
        "ok": video(13),
    }
    picked = to_download({}, videos, channel(keep=5, min_duration=60), NOW, 0)
    assert picked == ["premiere", "ok"]


def test_to_delete_keeps_newest():
    episodes = {"a": episode(10), "b": episode(20), "c": episode(30), "d": episode(40)}
    assert sorted(to_delete(episodes, 2)) == ["c", "d"]
    assert to_delete(episodes, 5) == []


def test_tabs():
    assert tabs(channel()) == ["videos"]
    assert tabs(channel(include_shorts=True, include_live=True)) == ["videos", "streams", "shorts"]


class FakeYtDlp:
    """Stands in for yt-dlp: a channel's tabs and videos, and downloads that write a file."""

    def __init__(self, tabs: dict, videos: dict):
        self.tabs = tabs
        self.videos = videos
        self.info_calls = []
        self.downloads = []

    def list_tab(self, url, count):
        tab = url.rsplit("/", 1)[-1]
        if tab not in self.tabs:
            return None
        if self.tabs[tab] == "error":
            raise YtDlpError("network down")
        return {"channel": "Test Channel", "description": "About", "channel_url": "https://yt/c",
                "thumbnails": [{"id": "avatar_uncropped", "url": "https://img/avatar.jpg"}],
                "entries": [{"id": vid, "duration": self.videos[vid].get("duration")}
                            for vid in self.tabs[tab][:count]]}

    def video_info(self, vid):
        self.info_calls.append(vid)
        return {"id": vid, **self.videos[vid]}

    def download(self, vid, dest_dir: Path, sponsorblock):
        self.downloads.append(vid)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{vid}.m4a"
        path.write_bytes(b"audio" * 10)
        return path


def info(age_hours, **extra):
    return {"title": "t", "timestamp": NOW - age_hours * HOUR, "duration": 1200, "live_status": "not_live",
            "thumbnails": [{"url": "https://i/hq.jpg", "width": 480}, {"url": "https://i/max.webp", "width": 1280},
                           {"url": "https://i/max.jpg?x=1", "width": 1280}], **extra}


@pytest.fixture
def now(monkeypatch):
    monkeypatch.setattr("tubecast.sync.time.time", lambda: NOW)
    monkeypatch.setattr("tubecast.sync.audio_duration", lambda path: 1100)


def test_sync_channel_end_to_end(tmp_path, now, monkeypatch):
    config = parse({"base_url": "http://x", "keep": 2, "download_delay": "6h", "include_live": True,
                    "channels": ["@Test"]})
    [ch] = config.channels
    yt = FakeYtDlp(
        tabs={"videos": ["v1", "v2", "v3"], "streams": ["s1"]},
        videos={"v1": info(2), "v2": info(10), "v3": info(30), "s1": info(20, live_status="was_live")})

    sync_channel(config, ch, yt, tmp_path)
    state = load_state(tmp_path, "test")
    assert yt.downloads == ["v2", "s1"]  # v1 is too new for SponsorBlock, v3 is the third newest
    assert sorted(state["episodes"]) == ["s1", "v2"]
    assert state["episodes"]["v2"]["duration"] == 1100
    assert state["episodes"]["v2"]["size"] == 50
    assert state["episodes"]["v2"]["thumbnail"] == "https://i/max.jpg?x=1"
    assert state["channel"]["title"] == "Test Channel"

    # Later: v1 is old enough now and pushes out s1, the oldest episode
    yt.info_calls.clear()
    monkeypatch.setattr("tubecast.sync.time.time", lambda: NOW + 5 * HOUR)
    sync_channel(config, ch, yt, tmp_path)
    state = load_state(tmp_path, "test")
    assert yt.info_calls == []  # nothing fetched again
    assert sorted(state["episodes"]) == ["v1", "v2"]
    assert sorted(p.name for p in (tmp_path / "audio" / "test").iterdir()) == ["v1.m4a", "v2.m4a"]


def test_sync_keeps_state_when_youtube_is_down(tmp_path, now):
    config = parse({"base_url": "http://x", "channels": ["@Test"]})
    [ch] = config.channels
    yt = FakeYtDlp(tabs={"videos": ["v1"]}, videos={"v1": info(10)})
    sync_channel(config, ch, yt, tmp_path)
    yt.tabs["videos"] = "error"
    sync_channel(config, ch, yt, tmp_path)
    state = load_state(tmp_path, "test")
    assert list(state["videos"]) == ["v1"]
    assert list(state["episodes"]) == ["v1"]


def test_sync_removes_leftover_files(tmp_path, now):
    config = parse({"base_url": "http://x", "channels": ["@Test"]})
    [ch] = config.channels
    folder = tmp_path / "audio" / "test"
    folder.mkdir(parents=True)
    (folder / "old.m4a.part").write_bytes(b"x")
    sync_channel(config, ch, FakeYtDlp(tabs={"videos": []}, videos={}), tmp_path)
    assert list(folder.iterdir()) == []


def test_failed_download_is_retried_later(tmp_path, now):
    config = parse({"base_url": "http://x", "channels": ["@Test"]})
    [ch] = config.channels
    yt = FakeYtDlp(tabs={"videos": ["v1"]}, videos={"v1": info(10)})

    def fail(*args):
        raise YtDlpError("Sign in to confirm you're not a bot")
    yt.download = fail
    sync_channel(config, ch, yt, tmp_path)
    state = load_state(tmp_path, "test")
    assert state["episodes"] == {}
    assert "bot" in state["videos"]["v1"]["error"]
