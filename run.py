"""Application server entrypoint (supports multi-threaded dev server & production Waitress WSGI)."""

from __future__ import annotations

import os
import sys

from backend import create_app


def main() -> None:
    app = create_app()
    host = app.config["APP_HOST"]
    port = int(app.config["APP_PORT"])
    env = app.config["APP_ENV"]

    use_waitress = "--prod" in sys.argv or env == "production" or os.getenv("USE_WAITRESS", "0") == "1"
    if use_waitress:
        from waitress import serve

        print(f"[VeloPass] Starting production WSGI server on http://{host}:{port} (threads=16)")
        serve(app, host=host, port=port, threads=16)
    else:
        print(f"[VeloPass] Starting development server on http://{host}:{port}")
        app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
