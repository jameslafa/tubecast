"""Starts tubecast: the HTTP server, and a loop that checks every channel."""

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from .config import ConfigError, load
from .server import App, make_server
from .sync import sync_all
from .ytdlp import YtDlp

log = logging.getLogger("tubecast")

UPDATE_EVERY = 24 * 3600  # seconds between two yt-dlp updates
POLL = 30  # seconds between two looks at config.yaml while waiting for the next check


def config_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return None


def main() -> None:
    logging.basicConfig(level=os.environ.get("TUBECAST_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Docker stops containers with SIGTERM, which Python ignores when it runs as PID 1
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    data_dir = Path(os.environ.get("TUBECAST_DATA_DIR", "/data"))
    config_path = data_dir / "config.yaml"
    update_ytdlp = os.environ.get("TUBECAST_UPDATE_YTDLP", "1") != "0"
    host = os.environ.get("TUBECAST_BIND", "0.0.0.0")
    port = int(os.environ.get("PORT", "8091"))

    app = App(data_dir)
    server = make_server(app, host, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("serving on %s:%d, data in %s", host, port, data_dir)

    ytdlp = YtDlp(data_dir)
    last_update = 0.0
    while True:
        if update_ytdlp and time.time() - last_update >= UPDATE_EVERY:
            try:
                ytdlp.update()
            except Exception as e:  # keep the yt-dlp we have
                log.warning("could not update yt-dlp: %s", e)
            last_update = time.time()

        mtime = config_mtime(config_path)
        try:
            app.config, app.config_error = load(config_path), None
        except ConfigError as e:
            log.error("config: %s", e)
            app.config_error = str(e)  # keep serving the last good config, if any
        if app.config and not app.config_error:
            sync_all(app.config, ytdlp, data_dir)
            wait = app.config.check_every
        else:
            wait = UPDATE_EVERY

        # Sleep until the next check, or until config.yaml changes so a new channel shows up soon
        deadline = time.time() + wait
        while time.time() < deadline and config_mtime(config_path) == mtime:
            time.sleep(min(POLL, max(deadline - time.time(), 0)))


if __name__ == "__main__":
    main()
