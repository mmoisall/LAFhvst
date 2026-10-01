"""GUI 창/트레이/서버 생명주기 제어.

GUI 모드(main.py)에서만 사용된다.
- 창 닫기(X) → 트레이로 숨기고 서버는 계속 실행
- 트레이 메뉴: 열기 / 서버 주소 / 종료
- 종료 시 uvicorn graceful shutdown 후 창을 파괴

pywebview 창 메서드는 winforms 백엔드에서 UI 스레드로 Invoke 되므로 다른
스레드(트레이 등)에서 호출해도 안전하다.
"""

from __future__ import annotations

import threading

try:  # 트레이(pystray)는 선택적 의존성
    import pystray
    from PIL import Image

    _TRAY_IMPORTABLE = True
except Exception:  # pragma: no cover - 환경 의존
    pystray = None
    Image = None
    _TRAY_IMPORTABLE = False

_FALLBACK_COLOR = (200, 100, 20, 255)


class AppControl:
    def __init__(self) -> None:
        self._window = None
        self._server = None
        self._server_thread = None
        self._icon_path = None
        self._server_info = None
        self._icon = None
        self._quitting = False
        self._lock = threading.Lock()

    # --- 등록 -----------------------------------------------------------
    def attach(self, window, server, server_thread, icon_path=None, server_info=None) -> None:
        self._window = window
        self._server = server
        self._server_thread = server_thread
        self._icon_path = icon_path
        self._server_info = server_info

    def attach_server(self, server, server_thread=None) -> None:
        """GUI가 없어도(서버 콘솔 모드) 종료 트리거를 위해 서버를 등록한다."""
        self._server = server
        if server_thread is not None:
            self._server_thread = server_thread

    def is_gui(self) -> bool:
        return self._window is not None

    @property
    def quitting(self) -> bool:
        return self._quitting

    def tray_available(self) -> bool:
        return _TRAY_IMPORTABLE and self._window is not None

    # --- 창 제어 --------------------------------------------------------
    def hide_to_tray(self) -> bool:
        if self._window is None:
            return False
        try:
            self._window.hide()
        except Exception:
            return False
        return True

    def show_window(self) -> None:
        if self._window is None:
            return
        try:
            self._window.show()
            try:
                self._window.restore()
            except Exception:
                pass
        except Exception:
            pass

    def _server_info_text(self) -> str:
        try:
            return self._server_info() if self._server_info else ""
        except Exception:
            return ""

    def _notify_server_info(self) -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(self._server_info_text(), "LAFhvst 서버 주소")
        except Exception:
            pass

    # --- 트레이 ---------------------------------------------------------
    def start_tray(self) -> bool:
        if not self.tray_available():
            return False
        try:
            image = Image.open(self._icon_path) if self._icon_path else None
        except Exception:
            image = None
        if image is None:
            image = Image.new("RGBA", (32, 32), _FALLBACK_COLOR)
        menu = pystray.Menu(
            pystray.MenuItem("열기", lambda icon, item: self.show_window(), default=True),
            pystray.MenuItem("서버 주소", lambda icon, item: self._notify_server_info()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", lambda icon, item: self.quit()),
        )
        try:
            self._icon = pystray.Icon("LAFhvst", image, "LAFhvst", menu)
            threading.Thread(target=self._icon.run, name="lafhvst-tray", daemon=True).start()
        except Exception:
            self._icon = None
            return False
        return True

    # --- 종료 -----------------------------------------------------------
    def quit(self) -> None:
        with self._lock:
            if self._quitting:
                return
            self._quitting = True
        try:
            if self._server is not None:
                self._server.should_exit = True
        except Exception:
            pass
        thread = self._server_thread
        if thread is not None:
            try:
                thread.join(timeout=5)
            except Exception:
                pass
        try:
            if self._icon is not None:
                self._icon.stop()
        except Exception:
            pass
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass


app_control = AppControl()
