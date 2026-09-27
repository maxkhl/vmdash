import logging
import os
import signal
import sys

from waitress import serve

from .config import Config
from .web import create_app


def main():
    logging.basicConfig(
        level=os.environ.get("VMDASH_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.from_env()
    app = create_app(cfg)
    # Als PID 1 im Container gibt es keinen Default-Handler für SIGTERM.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Genau ein Prozess: Jobs liegen im Arbeitsspeicher.
    serve(app, host="0.0.0.0", port=cfg.port, threads=8, ident="vmdash")


if __name__ == "__main__":
    main()
