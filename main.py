import multiprocessing
import os
import sys
import threading

import uvicorn

from core import app_control
from core.database import init_db
from core.utils import local_ipv4_addresses, resource_root
from server import app, wait_for_server

HOST = "0.0.0.0"
DEFAULT_PORT = 17363


def _resolve_port() -> int:
    argv = sys.argv
    for index, token in enumerate(argv):
        if token == "--port" and index + 1 < len(argv):
            candidate = argv[index + 1]
        elif token.startswith("--port="):
            candidate = token.split("=", 1)[1]
        else:
            continue
        try:
            return int(candidate)
        except ValueError:
            break
    try:
        return int(os.environ.get("LAF_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


PORT = _resolve_port()
WINDOW_URL = "http://127.0.0.1:" + str(PORT)


def _run_server(server: uvicorn.Server) -> None:
    server.run()


def _serve_only() -> bool:
    return "--serve" in sys.argv or os.environ.get("LAF_NO_GUI") == "1"


def _server_address_text() -> str:
    lines = [WINDOW_URL]
    for address in local_ipv4_addresses():
        lines.append("http://" + address + ":" + str(PORT))
    return "\n".join(lines)


def _close_to_tray_enabled() -> bool:
    control = app_control.app_control
    if not control.tray_available():
        return False
    try:
        from core import models

        data = models.get_app_settings() or {}
        return data.get("closeToTray", True) is not False
    except Exception:
        return True


def _on_closing() -> bool:
    """창 닫기: 트레이 상주 또는 종료. False 반환 시 닫기 취소."""
    control = app_control.app_control
    if control.quitting:
        return True
    if _close_to_tray_enabled():
        control.hide_to_tray()
        return False
    control.quit()
    return True


def main() -> None:
    init_db()

    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        log_level="info",
        loop="server:proactor_loop_factory",
    )
    server = uvicorn.Server(config)
    server_thread = threading.Thread(
        target=_run_server, args=(server,), name="lafhvst-server", daemon=True
    )
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
            server.should_exit = True
            server_thread.join(timeout=5)
        return

    import webview

    assets_dir = os.path.join(resource_root(), "assets")
    window_icon = os.path.join(assets_dir, "LAFhvst.ico")
    tray_icon = window_icon if os.path.isfile(window_icon) else os.path.join(assets_dir, "icon.png")
    window = webview.create_window(
        "LAFhvst", url=WINDOW_URL, width=1100, height=740, resizable=True
    )

    control = app_control.app_control
    control.attach(
        window, server, server_thread, icon_path=tray_icon, server_info=_server_address_text
    )
    control.start_tray()

    window.events.closing += _on_closing
    window.expose(control.hide_to_tray, control.show_window, control.quit)

    if os.path.isfile(window_icon):
        webview.start(icon=window_icon)
    else:
        webview.start()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
