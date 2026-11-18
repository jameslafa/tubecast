"""The "Listen later" feed: videos sent one by one to POST /later, downloaded right away.

Its state is data/state/later.json, laid out like a channel's, with:
- "pending": URLs sent but not downloaded yet, kept so a restart doesn't lose them
- "episodes": as a channel's, but "timestamp" is when the video was sent (the feed is sorted by
  it), plus "link", "uploader", "uploaded" (the video's own upload time) and "start" (the t= of
  the sent URL, in seconds, or None)
"""

import logging
import re
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import LATER_SLUG, Channel, Config
from .sync import audio_dir, episode, load_state, remove_orphans, save_state, video_meta
from .ytdlp import YtDlp, YtDlpError

log = logging.getLogger(__name__)

TITLE = "Listen later"
DESCRIPTION = "Videos sent to tubecast to listen to later."
DAY = 24 * 3600
URL = re.compile(r"https?://\S+")


def channel(config: Config) -> Channel:
    """The feed as a channel, for the feed builder and the server."""
    return Channel(url=config.base_url, slug=LATER_SLUG, keep=0, min_duration=0,
                   include_shorts=False, include_live=False)


def load(data_dir: Path) -> dict:
    state = load_state(data_dir, LATER_SLUG)
    state.setdefault("pending", [])
    state["channel"] = {"title": TITLE, "description": DESCRIPTION}
    return state


def find_url(text: str) -> str | None:
    """The first http(s) URL in a text: shared text sometimes comes with the video's title."""
    match = URL.search(text)
    return match.group(0) if match else None


def parse_start(url: str) -> int | None:
    """Seconds from a t= (or start=) in the URL's query or fragment: 754, 754s, 12m34s, 1h2m."""
    parts = urlsplit(url)
    params = {**parse_qs(parts.fragment), **parse_qs(parts.query)}
    value = (params.get("t") or params.get("start") or [""])[0].strip().lower()
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", value)
    if not value or not match:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds or None


def format_start(seconds: int) -> str:
    """12:34, or 1:02:03 past an hour."""
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def episode_id(info: dict) -> str:
    """The episode's key, GUID and file name: the video ID on YouTube, like channel episodes,
    "<site>-<id>" elsewhere."""
    site = info.get("extractor_key") or "video"
    vid = str(info["id"]) if site == "Youtube" else f"{site.lower()}-{info['id']}"
    return re.sub(r"[^\w-]", "_", vid)


def to_delete(episodes: dict, now: float, keep_days: int, max_size: int) -> list[str]:
    """Episodes sent more than keep_days ago, then the oldest while the rest is over max_size.
    The newest episode always stays."""
    oldest_first = sorted(episodes, key=lambda vid: episodes[vid]["timestamp"])
    expired = [vid for vid in oldest_first[:-1] if now - episodes[vid]["timestamp"] > keep_days * DAY]
    kept = [vid for vid in oldest_first if vid not in expired]
    size = sum(episodes[vid].get("size") or 0 for vid in kept)
    for vid in kept[:-1]:
        if size <= max_size:
            break
        expired.append(vid)
        size -= episodes[vid].get("size") or 0
    return expired


def short(error: str) -> str:
    """yt-dlp's error without its "ERROR: [site] id:" prefix and long advice, for a push."""
    error = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[^:\s]+:\s*)?", "", error)
    return error.split(". ", 1)[0][:200]


def notify(ntfy_url: str | None, message: str) -> None:
    """Posts a failure to an ntfy topic (a full URL like https://ntfy.sh/<topic>), if set."""
    if not ntfy_url:
        return
    request = urllib.request.Request(ntfy_url, data=message.encode(), method="POST",
                                     headers={"Title": "tubecast: Listen later failed", "Tags": "warning"})
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except Exception as e:
        log.warning("later: could not send the failure to ntfy: %s", e)


class Later:
    """Takes URLs from the server and downloads them one at a time in its own thread."""

    def __init__(self, data_dir: Path, ytdlp: YtDlp, get_config, ntfy_url: str | None = None,
                 retry_after: float = 60):
        self.data_dir = data_dir
        self.ytdlp = ytdlp
        self.get_config = get_config  # the config the scheduler loaded last, or None
        self.ntfy_url = ntfy_url
        self.retry_after = retry_after
        self.lock = threading.Lock()  # around each read and write of the state
        self.wake = threading.Event()

    def add(self, url: str) -> None:
        with self.lock:
            state = load(self.data_dir)
            if url not in (p["url"] for p in state["pending"]):
                state["pending"].append({"url": url, "sent": time.time()})
                save_state(self.data_dir, LATER_SLUG, state)
        log.info("later: queued %s", url)
        self.wake.set()

    def run(self) -> None:
        """The download thread: works through the pending URLs, then waits for the next one."""
        with self.lock:
            remove_orphans(audio_dir(self.data_dir, LATER_SLUG), load(self.data_dir)["episodes"])
        while True:
            self.wake.clear()
            config = self.get_config()
            pending = load(self.data_dir)["pending"] if config and config.later else []
            if not pending:
                self.wake.wait(60)  # the config may turn the feed on meanwhile
                continue
            item = pending[0]
            try:
                self.process(config, item["url"], item["sent"])
            except Exception:
                log.exception("later: %s failed", item["url"])
                self._done(item["url"])

    def process(self, config: Config, url: str, sent: float) -> None:
        """Downloads one URL, trying once more after retry_after, and sends a push if both fail."""
        title = None
        for attempt in (1, 2):
            try:
                info = self.ytdlp.url_info(url)
                title = info.get("title") or title
                self._add(config, url, sent, info)
                return
            except YtDlpError as e:
                log.warning("later: %s failed (attempt %d): %s", url, attempt, e)
                if attempt == 1:
                    time.sleep(self.retry_after)
                    continue
                notify(self.ntfy_url, f"{title or url}\n{short(str(e))}")
                self._done(url)
                with self.lock:  # what the failed download left, such as its thumbnail
                    remove_orphans(audio_dir(self.data_dir, LATER_SLUG), load(self.data_dir)["episodes"])

    def _add(self, config: Config, url: str, sent: float, info: dict) -> None:
        vid = episode_id(info)
        folder = audio_dir(self.data_dir, LATER_SLUG)
        start = parse_start(url)
        with self.lock:
            state = load(self.data_dir)
            known = state["episodes"].get(vid)
            if known and (folder / known["file"]).exists():
                # Still in the feed: back to the top, and its days start again
                known.update(timestamp=sent, start=start)
                self._save(config, state, url)
                log.info("later: %s %r is already in the feed, moved to the top", vid, known["title"])
                return

        log.info("later: downloading %s %r", vid, info.get("title"))
        path, segments = self.ytdlp.download(url, folder, config.sponsorblock, name=vid)
        meta = video_meta(info)
        with self.lock:
            state = load(self.data_dir)
            state["episodes"][vid] = {
                **episode(meta, path, segments),
                "timestamp": sent,
                "uploaded": meta["timestamp"],
                "uploader": info.get("channel") or info.get("uploader"),
                "link": info.get("webpage_url") or url,
                "start": start,
            }
            self._save(config, state, url)

    def _done(self, url: str) -> None:
        with self.lock:
            state = load(self.data_dir)
            state["pending"] = [p for p in state["pending"] if p["url"] != url]
            save_state(self.data_dir, LATER_SLUG, state)

    def _save(self, config: Config, state: dict, url: str) -> None:
        state["pending"] = [p for p in state["pending"] if p["url"] != url]
        self._prune(config, state)
        save_state(self.data_dir, LATER_SLUG, state)

    def _prune(self, config: Config, state: dict) -> None:
        episodes = state["episodes"]
        folder = audio_dir(self.data_dir, LATER_SLUG)
        for vid in to_delete(episodes, time.time(), config.later.keep_days, config.later.max_size):
            log.info("later: deleting %s %r", vid, episodes[vid]["title"])
            (folder / episodes.pop(vid)["file"]).unlink(missing_ok=True)

    def prune(self, config: Config) -> None:
        """Deletes the episodes past keep_days or max_size; the scheduler calls it each check."""
        if not config.later:
            return
        with self.lock:
            state = load(self.data_dir)
            self._prune(config, state)
            save_state(self.data_dir, LATER_SLUG, state)
