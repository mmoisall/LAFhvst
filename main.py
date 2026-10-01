import os
import sys
import threading

import uvicorn

from core.database import init_db
from core.utils import local_ipv4_addresses
from server import app, wait_for_server

HOST = "0.0.0.0"
PORT = 17363
WINDOW_URL = "http://127.0.0.1:" + str(PORT)


def _run_server() -> None:
    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
        loop="server:proactor_loop_factory",
    )
    uvicorn.Server(config).run()


def _serve_only() -> bool:
    return "--serve" in sys.argv or os.environ.get("LAF_NO_GUI") == "1"


def main() -> None:
    init_db()

    server_thread = threading.Thread(target=_run_server, name="lafhvst-server", daemon=True)
    server_thread.start()

    if not wait_for_server(PORT):
        raise RuntimeError("LAFhvst server did not start on port " + str(PORT))

    print("=" * 56)
    print(" LAFhvst engine started")
    print("   local:   " + WINDOW_URL)
    for address in local_ipv4_addresses():
        print("   network: http://" + address + ":" + str(PORT))
    print("=" * 56)

    if _serve_only():
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass
        return

    import webview

    webview.create_window("LAFhvst", url=WINDOW_URL, width=1100, height=740, resizable=True)
    webview.start()


if __name__ == "__main__":
    main()
