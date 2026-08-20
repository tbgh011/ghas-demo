#!/usr/bin/env python3
"""
Kepler — Planetary Image Processing Suite
Entry point
"""

import sys
import os

APP_NAME    = "Kepler"
APP_VERSION = "1.3.5"

if sys.version_info < (3, 8):
    print(f"ERROR: {APP_NAME} requires Python 3.8 or higher.")
    print(f"You have Python {sys.version}")
    sys.exit(1)


def _show_error(title, msg):
    """Show an error in a way that's always visible — even under pythonw."""
    try:
        import tkinter as _tk
        from tkinter import messagebox as _mb
        _r = _tk.Tk(); _r.withdraw()
        _mb.showerror(title, msg)
        _r.destroy()
    except Exception:
        pass  # tkinter itself broken — nothing we can do silently
    # Also write to a log file next to main.py so the user can find it
    try:
        log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kepler_error.log")
        with open(log, "w") as f:
            f.write(f"{title}\n\n{msg}\n")
    except Exception:
        pass


def check_dependencies():
    missing = []
    for mod, pip_name in [("numpy", "numpy"), ("scipy", "scipy"),
                           ("tifffile", "tifffile")]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pip_name)
    try:
        from PIL import Image
    except ImportError:
        missing.append("Pillow")
    try:
        import tkinter
    except ImportError:
        _show_error(
            f"{APP_NAME} — Missing dependency",
            "tkinter is not installed.\n\n"
            + ("Install with:  sudo apt install python3-tk"
               if sys.platform.startswith("linux")
               else ("Install with:  brew install python-tk\n"
                     "Or reinstall Python from https://www.python.org/downloads/macos/"
                     if sys.platform == "darwin"
                     else "Re-install Python and make sure to include tcl/tk."))
        )
        sys.exit(1)

    if missing:
        pip_cmd = f"pip install {' '.join(missing)}"
        _show_error(
            f"{APP_NAME} — Missing packages",
            f"The following packages are required but not installed:\n\n"
            f"  {', '.join(missing)}\n\n"
            f"Fix by running:\n  {pip_cmd}\n\n"
            + ("Or re-run:  installer\\windows\\install.bat"
               if sys.platform == "win32"
               else ("Or re-run:  bash installer/macos/install.sh"
                     if sys.platform == "darwin"
                     else "Or re-run:  bash installer/linux/install.sh"))
        )
        sys.exit(1)


def load_icon(root):
    """
    Load the program icon cross-platform.
    On Windows: iconbitmap() with .ico — deferred via after(0) so the window
    is fully initialized before we apply it (silent failure otherwise).
    On Linux/macOS: iconphoto() via Pillow.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    ico  = os.path.join(here, "icon.ico")

    if not os.path.isfile(ico):
        return

    def _apply():
        if sys.platform == "win32":
            try:
                root.iconbitmap(default=ico)
                return
            except Exception:
                pass

        # Linux / macOS (or Windows fallback) — Pillow → iconphoto
        try:
            from PIL import Image, ImageTk
            img   = Image.open(ico)
            sizes = img.info.get("sizes", {img.size})
            best  = max(sizes, key=lambda s: s[0])
            img   = img.resize(best, Image.LANCZOS).convert("RGBA")
            photo = ImageTk.PhotoImage(img)
            root.iconphoto(True, photo)
            root._icon_img = photo  # prevent GC
        except Exception:
            pass

    # Defer so the window handle exists before we try to set the icon
    root.after(0, _apply)


check_dependencies()

try:
    from app import KeplerApp
except Exception as _e:
    import traceback as _tb
    _show_error(f"{APP_NAME} — Startup error",
                f"Failed to load app.py:\n\n{_e}\n\n{_tb.format_exc()}")
    sys.exit(1)

import tkinter as tk

if __name__ == "__main__":
    try:
        root = tk.Tk()
        root.title(f"{APP_NAME} v{APP_VERSION} — Planetary Image Processing Suite")
        root.resizable(True, True)  # must be set early on macOS
        # Bring window to front on all platforms
        root.lift()
        root.focus_force()
        root.attributes("-topmost", True)
        root.after(500, lambda: root.attributes("-topmost", False))

        # Windows taskbar grouping
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    f"kepler.planetary.{APP_VERSION}"
                )
        except Exception:
            pass

        # DPI awareness (Windows)
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        # macOS tweaks
        if sys.platform == "darwin":
            # Silence the "system version of Tk is deprecated" warning
            os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

        load_icon(root)

        app = KeplerApp(root)
        root.mainloop()

    except Exception as _e:
        import traceback as _tb
        _show_error(f"{APP_NAME} — Crash",
                    f"Kepler crashed on startup:\n\n{_e}\n\n{_tb.format_exc()}")
