"""Reads config.yaml: global settings and the list of channels to follow."""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

# Channel options that can also be set at the top level, as defaults for every channel
CHANNEL_DEFAULTS = {"keep": 5, "min_duration": 0, "include_shorts": False, "include_live": False}
GLOBAL_DEFAULTS = {
    "check_every": "4h",
    "download_delay": "6h",
    "sponsorblock": ["sponsor", "selfpromo", "interaction"],
}
# Tabs yt-dlp accepts at the end of a channel URL; we strip them to get the channel itself
CHANNEL_TABS = {"featured", "videos", "shorts", "streams", "live", "playlists", "community",
                "releases", "podcasts", "about"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Channel:
    url: str  # channel URL without a tab, e.g. https://www.youtube.com/@GamersNexus
    slug: str  # name used in the feed and audio URLs
    keep: int
    min_duration: int  # seconds, 0 for no minimum
    include_shorts: bool
    include_live: bool


@dataclass(frozen=True)
class Config:
    base_url: str  # where podcast apps reach tubecast, used in feed and audio URLs
    check_every: int  # seconds between two checks of all channels
    download_delay: int  # seconds a video must be online before we download it
    sponsorblock: tuple[str, ...]
    channels: tuple[Channel, ...]


def parse_duration(value, key: str) -> int:
    """Seconds from a number of seconds or a string like "6h", "90m", "1h30m" or "45s"."""
    if isinstance(value, bool):
        raise ConfigError(f"{key}: expected a duration like 6h or 30m, got {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ConfigError(f"{key}: must not be negative")
        return value
    text = str(value).strip().lower()
    if text.isdigit():
        return int(text)
    match = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", text)
    if not text or not match:
        raise ConfigError(f"{key}: expected a duration like 6h or 30m, got {value!r}")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def normalize_channel_url(value: str) -> str:
    """The channel's own URL from a handle, a channel ID or any channel URL (tabs stripped)."""
    text = value.strip()
    if text.startswith("@"):
        return f"https://www.youtube.com/{text}"
    if re.fullmatch(r"UC[\w-]{22}", text):
        return f"https://www.youtube.com/channel/{text}"
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host != "youtube.com":
        raise ConfigError(f"not a YouTube channel URL: {value!r}")
    segments = [s for s in parts.path.split("/") if s]
    if segments and segments[-1].lower() in CHANNEL_TABS:
        segments.pop()
    valid = (len(segments) == 1 and segments[0].startswith("@")) or (
        len(segments) == 2 and segments[0] in ("channel", "c", "user"))
    if not valid:
        raise ConfigError(f"not a YouTube channel URL: {value!r}")
    return "https://www.youtube.com/" + "/".join(segments)


def default_slug(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1].removeprefix("@")
    return re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")


def _bool(value, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{key}: expected true or false, got {value!r}")
    return value


def _keep(value, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{key}: expected a whole number of at least 1, got {value!r}")
    return value


def _channel(entry, defaults: dict, index: int) -> Channel:
    where = f"channels[{index}]"
    if isinstance(entry, str):
        entry = {"url": entry}
    if not isinstance(entry, dict) or "url" not in entry:
        raise ConfigError(f"{where}: expected a channel URL or a mapping with a url")
    unknown = set(entry) - {"url", "slug", *CHANNEL_DEFAULTS}
    if unknown:
        raise ConfigError(f"{where}: unknown option(s): {', '.join(sorted(unknown))}")
    options = {**defaults, **entry}
    url = normalize_channel_url(str(entry["url"]))
    slug = str(entry.get("slug") or default_slug(url))
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug):
        raise ConfigError(f"{where}: slug may only use a-z, 0-9, - and _, got {slug!r}")
    return Channel(
        url=url,
        slug=slug,
        keep=_keep(options["keep"], f"{where}.keep"),
        min_duration=parse_duration(options["min_duration"], f"{where}.min_duration"),
        include_shorts=_bool(options["include_shorts"], f"{where}.include_shorts"),
        include_live=_bool(options["include_live"], f"{where}.include_live"),
    )


def parse(data) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("the config must be a mapping of settings")
    unknown = set(data) - {"base_url", "channels", *GLOBAL_DEFAULTS, *CHANNEL_DEFAULTS}
    if unknown:
        raise ConfigError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    base_url = str(data.get("base_url") or "").rstrip("/")
    if not re.match(r"https?://[^/]", base_url):
        raise ConfigError("base_url: required, e.g. https://mac-mini.example.ts.net:8451")
    options = {**GLOBAL_DEFAULTS, **data}
    defaults = {key: options.get(key, value) for key, value in CHANNEL_DEFAULTS.items()}

    sponsorblock = options["sponsorblock"] or []
    if not isinstance(sponsorblock, list) or not all(isinstance(c, str) for c in sponsorblock):
        raise ConfigError("sponsorblock: expected a list of categories, e.g. [sponsor, selfpromo]")

    entries = data.get("channels") or []
    if not isinstance(entries, list):
        raise ConfigError("channels: expected a list")
    channels = [_channel(entry, defaults, i) for i, entry in enumerate(entries)]
    slugs = [c.slug for c in channels]
    duplicates = sorted({s for s in slugs if slugs.count(s) > 1})
    if duplicates:
        raise ConfigError(f"two channels share the slug {duplicates[0]!r}; set slug on one of them")

    check_every = parse_duration(options["check_every"], "check_every")
    if check_every < 60:
        raise ConfigError("check_every: must be at least 1m")
    return Config(
        base_url=base_url,
        check_every=check_every,
        download_delay=parse_duration(options["download_delay"], "download_delay"),
        sponsorblock=tuple(sponsorblock),
        channels=tuple(channels),
    )


def load(path: Path) -> Config:
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"{path} not found; copy config.example.yaml there") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from None
    return parse(data)
