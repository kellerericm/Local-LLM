"""Start LocalAgent: `python -m localagent [--port 8765] [--no-browser] [--fake-model]`."""
from __future__ import annotations

import argparse
import logging
import threading
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(prog="localagent")
    parser.add_argument("--host", help="Bind address (default from settings, 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Port (default from settings, 8765)")
    parser.add_argument("--data-dir", help="Data directory (default D:\\LocalAgent\\data or LOCALAGENT_DATA_DIR)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open the browser")
    parser.add_argument("--fake-model", action="store_true", help="Use a fake model (no GPU) to try the app")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    import uvicorn

    from .config import load_settings
    from .server import create_app

    settings = load_settings(args.data_dir)
    backend = None
    if args.fake_model:
        from .backend.fake import EchoBackend
        backend = EchoBackend()
    app = create_app(settings, backend)
    host = args.host or settings.host
    port = args.port or settings.port
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
