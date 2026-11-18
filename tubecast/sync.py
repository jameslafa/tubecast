"""One check of a channel: list its newest uploads, download what's missing, delete old episodes.

Each channel's state is a JSON file in data/state/<slug>.json:
- "channel": title, description, artwork and URL, for the feed
- "videos": metadata of the videos recently listed on the channel, so each one is fetched once
- "episodes": the downloaded episodes, which make up the feed
"""

import calendar
import json
import logging
import os
import time
from pathlib import Path

from .config import Channel, Config
from .chapters import merge, shift_timestamps
from .ytdlp import YtDlp, YtDlpError, audio_duration, file_chapters, watch_url

log = logging.getLogger(__name__)

MARGIN = 10  # extra entries listed per tab, for videos the filters skip
RETRY_AFTER = 24 * 3600  # seconds before trying again a video that failed
NOT_READY = {"is_live", "is_upcoming", "post_live"}  # live_status values to look at again later


def state_path(data_dir: Path, slug: str) -> Path:
    return data_dir / "state" / f"{slug}.json"


def audio_dir(data_dir: Path, slug: str) -> Path:
    return data_dir / "audio" / slug


def load_state(data_dir: Path, slug: str) -> dict:
    try:
        state = json.loads(state_path(data_dir, slug).read_text())
    except FileNotFoundError:
        state = {}
    return {"channel": {}, "videos": {}, "episodes": {}, **state}


def save_state(data_dir: Path, slug: str, state: dict) -> None:
    path = state_path(data_dir, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    os.replace(tmp, path)


def tabs(channel: Channel) -> list[str]:
    """The channel tabs to list. Regular uploads (premieres too) are in "videos", Shorts and
    live streams in their own tabs."""
    names = ["videos"]
    if channel.include_live:
        names.append("streams")
    if channel.include_shorts:
        names.append("shorts")
    return names


def channel_info(data: dict, channel: Channel) -> dict:
    thumbnails = data.get("thumbnails") or []
    avatar = next((t["url"] for t in thumbnails if t.get("id") == "avatar_uncropped"), None)
    return {
        "title": data.get("channel") or data.get("uploader") or channel.slug,
        "description": data.get("description") or "",
        "url": data.get("channel_url") or channel.url,
        "image": avatar,
    }


def video_meta(info: dict) -> dict:
    timestamp = info.get("release_timestamp") or info.get("timestamp")
    if not timestamp and info.get("upload_date"):
        timestamp = calendar.timegm(time.strptime(info["upload_date"], "%Y%m%d"))
    return {
        "title": info.get("title") or info["id"],
        "description": info.get("description") or "",
        "timestamp": timestamp,
        "duration": info.get("duration"),
        "live_status": info.get("live_status"),
        "thumbnail": jpeg_thumbnail(info),
    }


def jpeg_thumbnail(info: dict) -> str | None:
    """The largest JPEG thumbnail: podcast apps don't all show the WebP ones YouTube prefers."""
    jpegs = [t for t in info.get("thumbnails") or []
             if t.get("width") and t.get("url", "").split("?")[0].endswith(".jpg")]
    if jpegs:
        return max(jpegs, key=lambda t: t["width"])["url"]
    return info.get("thumbnail")


def is_eligible(meta: dict, channel: Channel, now: float, delay: int) -> bool:
    """Whether a listed video should be an episode: passes the filters and is old enough."""
    if "error" in meta or not meta.get("timestamp"):
        return False
    if now - meta["timestamp"] < delay:  # give SponsorBlock viewers time to mark segments
        return False
    if channel.min_duration and (meta.get("duration") or 0) < channel.min_duration:
        return False
    return True


def newest(timestamps: dict[str, float], count: int) -> list[str]:
    return sorted(timestamps, key=lambda vid: (timestamps[vid], vid), reverse=True)[:count]


def to_download(episodes: dict, videos: dict, channel: Channel, now: float, delay: int) -> list[str]:
    """Videos to download, newest first: those among the newest `keep` of episodes and
    eligible videos that are not episodes yet. Considering the episodes too means we never
    download a video that would be deleted right away for being older than all of them."""
    pool = {vid: m["timestamp"] for vid, m in videos.items() if is_eligible(m, channel, now, delay)}
    pool.update({vid: e["timestamp"] for vid, e in episodes.items()})
    return [vid for vid in newest(pool, channel.keep) if vid not in episodes]


def to_delete(episodes: dict, keep: int) -> list[str]:
    """Episodes beyond the newest `keep`."""
    kept = set(newest({vid: e["timestamp"] for vid, e in episodes.items()}, keep))
    return [vid for vid in episodes if vid not in kept]


def refresh_videos(state: dict, channel: Channel, ytdlp: YtDlp, now: float) -> None:
    """Lists the channel's tabs and fetches metadata for the videos we don't know yet."""
    listed = {}
    failed = 0
    for tab in tabs(channel):
        try:
            data = ytdlp.list_tab(f"{channel.url}/{tab}", channel.keep + MARGIN)
        except YtDlpError as e:
            log.warning("%s: could not list %s: %s", channel.slug, tab, e)
            failed += 1
            continue
        if data is None:
            continue
        state["channel"] = channel_info(data, channel)
        for entry in data.get("entries") or []:
            if entry.get("id") and entry.get("live_status") not in NOT_READY:
                listed[entry["id"]] = entry
    if failed == len(tabs(channel)):
        return  # YouTube unreachable: keep what we know and try again next check

    videos = state["videos"]
    for vid, entry in listed.items():
        known = videos.get(vid)
        if known and ("error" not in known or now - known["checked"] < RETRY_AFTER):
            continue
        # Flat listings give a duration but no upload time: skip the fetch if it's too short
        if channel.min_duration and (entry.get("duration") or channel.min_duration) < channel.min_duration:
            videos[vid] = {"error": "shorter than min_duration", "checked": now}
            continue
        try:
            info = ytdlp.video_info(vid)
        except YtDlpError as e:
            log.warning("%s: could not get %s: %s", channel.slug, vid, e)
            videos[vid] = {"error": str(e), "checked": now}
            continue
        if info.get("live_status") in NOT_READY:
            videos.pop(vid, None)
        else:
            videos[vid] = video_meta(info)
    # Forget videos no longer in the listing, so the state stays small
    state["videos"] = {vid: meta for vid, meta in videos.items() if vid in listed}


def remove_orphans(folder: Path, episodes: dict) -> None:
    """Deletes files no episode points to, such as partial downloads from a restart."""
    if not folder.is_dir():
        return
    files = {e["file"] for e in episodes.values()}
    for path in folder.iterdir():
        if path.is_file() and path.name not in files:
            log.info("removing leftover file %s", path)
            path.unlink()


def episode(meta: dict, path: Path, segments: list[tuple[float, float]]) -> dict:
    """An episode from a video's metadata and its downloaded audio."""
    return {
        **{k: meta[k] for k in ("title", "timestamp", "thumbnail")},
        # The chapter list in the description, moved to match the cut audio
        "description": shift_timestamps(meta["description"], merge(segments)),
        "duration": audio_duration(path) or meta.get("duration"),
        "chapters": file_chapters(path),
        "file": path.name,
        "size": path.stat().st_size,
    }


def sync_channel(config: Config, channel: Channel, ytdlp: YtDlp, data_dir: Path) -> None:
    now = time.time()
    state = load_state(data_dir, channel.slug)
    episodes = state["episodes"]
    folder = audio_dir(data_dir, channel.slug)
    remove_orphans(folder, episodes)

    refresh_videos(state, channel, ytdlp, now)
    save_state(data_dir, channel.slug, state)

    added = 0
    for vid in to_download(episodes, state["videos"], channel, now, config.download_delay):
        meta = state["videos"][vid]
        log.info("%s: downloading %s %r", channel.slug, vid, meta["title"])
        try:
            path, segments = ytdlp.download(watch_url(vid), folder, config.sponsorblock)
        except YtDlpError as e:
            log.warning("%s: could not download %s: %s", channel.slug, vid, e)
            state["videos"][vid] = {"error": f"download: {e}", "checked": now}
            save_state(data_dir, channel.slug, state)
            continue
        episodes[vid] = episode(meta, path, segments)
        added += 1
        save_state(data_dir, channel.slug, state)

    deleted = to_delete(episodes, channel.keep)
    for vid in deleted:
        (folder / episodes.pop(vid)["file"]).unlink(missing_ok=True)
    save_state(data_dir, channel.slug, state)
    log.info("%s: %d episodes (%d new, %d deleted)", channel.slug, len(episodes), added, len(deleted))


def sync_all(config: Config, ytdlp: YtDlp, data_dir: Path) -> None:
    for channel in config.channels:
        try:
            sync_channel(config, channel, ytdlp, data_dir)
        except Exception:
            log.exception("%s: check failed", channel.slug)
