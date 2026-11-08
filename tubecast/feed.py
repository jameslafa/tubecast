"""Builds a channel's podcast feed: RSS 2.0 with the iTunes tags podcast apps read."""

import html
from email.utils import format_datetime
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from .config import Channel
from .ytdlp import watch_url

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
ATOM = "http://www.w3.org/2005/Atom"
for prefix, uri in (("itunes", ITUNES), ("content", CONTENT), ("atom", ATOM)):
    ET.register_namespace(prefix, uri)


def feed_url(base_url: str, slug: str) -> str:
    return f"{base_url}/feeds/{slug}.xml"


def audio_url(base_url: str, slug: str, file: str) -> str:
    return f"{base_url}/audio/{slug}/{file}"


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs) -> ET.Element:
    element = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        element.text = text
    return element


def _html(description: str, link: str) -> str:
    """The description as simple HTML: paragraphs and line breaks kept, then the video link."""
    paragraphs = [p for p in description.split("\n\n") if p.strip()]
    body = "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)
    return body + f'<p><a href="{html.escape(link)}">Watch on YouTube</a></p>'


def build(channel: Channel, state: dict, base_url: str) -> bytes:
    info = state.get("channel") or {}
    title = info.get("title") or channel.slug
    link = info.get("url") or channel.url

    rss = ET.Element("rss", version="2.0")
    ch = _sub(rss, "channel")
    _sub(ch, "title", title)
    _sub(ch, "link", link)
    _sub(ch, "description", info.get("description") or title)
    _sub(ch, f"{{{ATOM}}}link", href=feed_url(base_url, channel.slug), rel="self",
         type="application/rss+xml")
    _sub(ch, "generator", "tubecast")
    _sub(ch, f"{{{ITUNES}}}author", title)
    _sub(ch, f"{{{ITUNES}}}explicit", "false")
    # A private feed: ask directories not to list it
    _sub(ch, f"{{{ITUNES}}}block", "Yes")
    if info.get("image"):
        _sub(ch, f"{{{ITUNES}}}image", href=info["image"])
        image = _sub(ch, "image")
        _sub(image, "url", info["image"])
        _sub(image, "title", title)
        _sub(image, "link", link)

    episodes = state.get("episodes") or {}
    for vid in sorted(episodes, key=lambda v: episodes[v]["timestamp"], reverse=True):
        episode = episodes[vid]
        video_link = watch_url(vid)
        item = _sub(ch, "item")
        _sub(item, "title", episode["title"])
        _sub(item, "link", video_link)
        _sub(item, "description", f"{episode['description']}\n\n{video_link}".strip())
        _sub(item, f"{{{CONTENT}}}encoded", _html(episode["description"], video_link))
        # The video ID, so apps keep the same episode if the file is downloaded again
        _sub(item, "guid", vid, isPermaLink="false")
        published = datetime.fromtimestamp(episode["timestamp"], timezone.utc)
        _sub(item, "pubDate", format_datetime(published))
        _sub(item, "enclosure", url=audio_url(base_url, channel.slug, episode["file"]),
             length=episode["size"], type="audio/mp4")
        if episode.get("duration"):
            _sub(item, f"{{{ITUNES}}}duration", str(int(episode["duration"])))
        if episode.get("thumbnail"):
            _sub(item, f"{{{ITUNES}}}image", href=episode["thumbnail"])

    ET.indent(rss)
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)
