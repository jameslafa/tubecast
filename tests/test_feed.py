from xml.etree import ElementTree as ET

from tubecast.config import Channel
from tubecast.feed import ITUNES, build

CHANNEL = Channel(url="https://www.youtube.com/@Test", slug="test", keep=5, min_duration=0,
                  include_shorts=False, include_live=False)
STATE = {
    "channel": {"title": "Test & Co", "description": "About us", "url": "https://yt/c",
                "image": "https://img/avatar.jpg"},
    "episodes": {
        "older": {"title": "Older", "description": "", "timestamp": 1_700_000_000, "duration": 60,
                  "thumbnail": None, "file": "older.m4a", "size": 1000},
        "newer": {"title": "Newer <b>", "description": "Line one\nline two\n\nPara", "duration": 3599.6,
                  "timestamp": 1_800_000_000, "thumbnail": "https://i/max.jpg", "file": "newer.m4a",
                  "size": 2000},
    },
}


def parse(state=STATE):
    return ET.fromstring(build(CHANNEL, state, "https://tube.example"))


def test_channel():
    ch = parse().find("channel")
    assert ch.findtext("title") == "Test & Co"
    assert ch.findtext("description") == "About us"
    assert ch.find(f"{{{ITUNES}}}image").get("href") == "https://img/avatar.jpg"
    assert ch.findtext(f"{{{ITUNES}}}block") == "Yes"


def test_episodes_newest_first():
    items = parse().find("channel").findall("item")
    assert [i.findtext("guid") for i in items] == ["newer", "older"]
    newer = items[0]
    assert newer.findtext("title") == "Newer <b>"
    assert newer.find("guid").get("isPermaLink") == "false"
    assert newer.findtext("pubDate") == "Fri, 15 Jan 2027 08:00:00 +0000"
    assert newer.findtext(f"{{{ITUNES}}}duration") == "3599"
    assert newer.find(f"{{{ITUNES}}}image").get("href") == "https://i/max.jpg"
    enclosure = newer.find("enclosure")
    assert enclosure.attrib == {"url": "https://tube.example/audio/test/newer.m4a", "length": "2000",
                                "type": "audio/mp4"}
    assert newer.findtext("description").endswith("https://www.youtube.com/watch?v=newer")
    html = newer.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
    assert html.startswith("<p>Line one<br>line two</p><p>Para</p>")


def test_before_first_check():
    ch = parse({"channel": {}, "episodes": {}}).find("channel")
    assert ch.findtext("title") == "test"
    assert ch.findall("item") == []
