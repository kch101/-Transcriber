"""Точка входа: поднимает локальный сервер и открывает нативное окно macOS.
Без pywebview (или с флагом --browser) открывает интерфейс в браузере."""
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ffmpeg из Homebrew, когда приложение запущено из Finder с урезанным PATH
os.environ["PATH"] = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "")])
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

APP_NAME = "Расшифровка"
ICON = Path(__file__).resolve().parent / "assets" / "icon.png"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int):
    import uvicorn
    from server import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def wait_ready(port: int, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class Api:
    """Методы доступны из JS как window.pywebview.api.*"""

    def __init__(self):
        self.window = None

    def pick_file(self):
        import webview
        res = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=False)
        return res[0] if res else None

    def save(self, fmt: str, payload: dict):
        import webview
        import export
        name = f"{payload.get('title') or 'Расшифровка'}.{fmt}"
        res = self.window.create_file_dialog(webview.FileDialog.SAVE, directory=str(Path.home() / "Documents"),
                                             save_filename=name)
        if not res:
            return None
        path = res if isinstance(res, str) else res[0]
        if not path.lower().endswith("." + fmt):
            path += "." + fmt
        Path(path).write_bytes(export.render(fmt, payload))
        return path

    def reveal(self, path: str):
        subprocess.run(["open", "-R", path])


def brand_macos():
    """Имя в меню и иконка в Dock вместо «Python»."""
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle
        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = APP_NAME
        if ICON.exists():
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initByReferencingFile_(str(ICON)))
    except Exception:
        pass


def main():
    port = free_port()
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    if not wait_ready(port):
        sys.exit("Сервер не запустился")
    url = f"http://127.0.0.1:{port}/"

    use_browser = "--browser" in sys.argv
    if not use_browser:
        try:
            import webview
        except ImportError:
            use_browser = True

    from server import jobs
    try:
        if use_browser:
            import webbrowser
            webbrowser.open(url)
            print(f"{APP_NAME}: {url}  (Ctrl+C для выхода)")
            while True:
                time.sleep(3600)
        else:
            api = Api()
            brand_macos()
            api.window = webview.create_window(APP_NAME, url, js_api=api, width=1180, height=820,
                                               min_size=(720, 560), background_color="#140802")
            webview.start(debug="--debug" in sys.argv)
    except KeyboardInterrupt:
        pass
    finally:
        jobs.shutdown()


if __name__ == "__main__":
    main()
