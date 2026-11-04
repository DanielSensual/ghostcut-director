"""GhostCut Director as a Mac window: the engine on a free local port, the UI in a native WebKit view.

    python app/main.py      # a Python with pywebview (and guestlist.meta for Muse) installed

Needs pywebview (`pip install pywebview`). Closing the window stops the engine.
"""
from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402

ICON = engine.ROOT / "docs" / "brand" / "ghostcut-icon-1024.png"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def brand_the_process() -> None:
    """Menu-bar name and Dock icon. A packaged .app gets these from its bundle; a script has to set them."""
    try:
        from AppKit import NSApplication, NSBundle, NSImage

        info = NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = "GhostCut Director"
        image = NSImage.alloc().initWithContentsOfFile_(str(ICON))
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:  # cosmetic only; never block the app over it
        pass


def main() -> None:
    import webview

    rest = list(sys.argv[1:])
    job = None
    if "--job" in rest:
        at = rest.index("--job")
        job = rest[at + 1] if at + 1 < len(rest) else None
        if not job:
            raise SystemExit("main: --job needs a directory")
    port = free_port()
    engine.export_saved_keys()
    httpd = engine.serve(port, job=job)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    brand_the_process()
    title = f"GhostCut Director — {Path(job).name}" if job else "GhostCut Director"
    webview.create_window(title, f"http://127.0.0.1:{port}/app/ui/", width=1440, height=900,
                          min_size=(1000, 680), background_color="#0b0d12")
    webview.start()
    httpd.shutdown()


if __name__ == "__main__":
    main()
