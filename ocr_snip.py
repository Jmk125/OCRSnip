"""
OCR Snip Tool  (v4)
-------------------
Runs in the background. Press CTRL+ALT+S, then drag a box around text
on screen. The text is OCR'd, formatted, copied to your clipboard, and
shown in a notification popup (and printed to the terminal when one
exists).

ROTATED TEXT: always drag from the text's top-left to its bottom-right
*relative to how the text reads*:

    drag down-right  -> normal text
    drag up-right    -> text reads bottom-to-top (rotated 90 CCW)
    drag down-left   -> text reads top-to-bottom (rotated 90 CW)
    drag up-left     -> upside-down text

FOLDER LAYOUT (portable / exe-ready):
    ocr_snip.py  (or ocr_snip.exe)
    tesseract/
        tesseract.exe
        tessdata/
        ... (rest of the Tesseract install)

    Tip: install Tesseract normally, then copy everything from
    C:\\Program Files\\Tesseract-OCR into the "tesseract" folder.

Setup:
    pip install keyboard pillow pytesseract pyperclip

Build exe:
    pip install pyinstaller
    pyinstaller --onefile --noconsole --name "OCR Snip" ocr_snip.py
    (then place the tesseract folder next to the built exe)

Exit:
    CTRL+ALT+Q
"""

import ctypes
import ctypes.wintypes
import os
import queue
import re
import sys
import threading
import tkinter as tk

if sys.platform != "win32":
    import keyboard
import pyperclip
import pytesseract
from PIL import Image, ImageGrab, ImageOps

# ---- HOTKEYS / OCR CONFIG ---------------------------------------------
HOTKEY_SNIP = "ctrl+alt+s"
HOTKEY_QUIT = "ctrl+alt+q"
UPSCALE = 3                    # upscale factor before OCR
TESSERACT_CONFIG = "--psm 6"   # try "--psm 7" if you mostly grab single lines

# ---- OUTPUT FORMATTING --------------------------------------------------
JOIN_LINES = True              # True: collapse multi-line captures to one line
JOIN_SEPARATOR = " "           # what goes between joined lines: " " or ", "

# CASE_MODE: "original" | "title" | "sentence" | "upper" | "lower"
CASE_MODE = "title"

# When CASE_MODE = "title": words this many letters or shorter stay ALL
# CAPS (keeps OH, NE, ST intact). 0 disables.
TITLE_CASE_KEEP_SHORT_UPPER = 2

# ---- NOTIFICATION POPUP -------------------------------------------------
POPUP_DURATION_MS = 4000       # how long the popup stays up (click to dismiss early)
POPUP_MAX_CHARS = 400          # truncate very long captures in the popup
POPUP_WIDTH = 360              # popup width in pixels

# ---- DEBUGGING ----------------------------------------------------------
# Set True to print each monitor's rectangle and the resulting overlay /
# canvas size. Useful for diagnosing multi-monitor / mixed-DPI coverage.
DEBUG_OVERLAY = False
# -------------------------------------------------------------------------

# --- Resolve tesseract relative to this script / exe ---------------------
# Works both as a plain .py and as a PyInstaller --onefile exe.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)      # folder containing the exe
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TESSERACT_PATH = os.path.join(BASE_DIR, "tesseract", "tesseract.exe")
pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
# -------------------------------------------------------------------------

# --- Make the process DPI-aware (fixes offset captures on scaled displays)
# NOTE: If a manifest (e.g. the one PyInstaller bakes into the .exe) already
# declared a DPI-awareness level, that level is LOCKED for the life of the
# process and every call below fails silently. That is why a scaled external
# monitor can still get a short/offset overlay in the built exe even though
# this code asks for per-monitor-v2. See OCRSnip.manifest / build-ocrsnip.bat.
_DPI_AWARENESS_SET = False
if sys.platform == "win32":
    try:
        # Per-monitor v2 also keeps Tk's coordinates aligned with monitors that
        # use different display scaling values.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _DPI_AWARENESS_SET = True
    except Exception:
        pass
    if not _DPI_AWARENESS_SET:
        try:
            # S_OK == 0. Non-zero (e.g. E_ACCESSDENIED) means it was already set.
            if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
                _DPI_AWARENESS_SET = True
        except Exception:
            pass
    if not _DPI_AWARENESS_SET:
        try:
            if ctypes.windll.user32.SetProcessDPIAware():
                _DPI_AWARENESS_SET = True
        except Exception:
            pass


def describe_dpi_awareness():
    """Return the process's *effective* DPI-awareness for diagnostics."""
    if sys.platform != "win32":
        return "n/a"
    names = {
        ctypes.c_void_p(-1).value: "UNAWARE",
        ctypes.c_void_p(-2).value: "SYSTEM_AWARE",
        ctypes.c_void_p(-3).value: "PER_MONITOR_AWARE",
        ctypes.c_void_p(-4).value: "PER_MONITOR_AWARE_V2",
        ctypes.c_void_p(-5).value: "UNAWARE_GDISCALED",
    }
    try:
        ctx = ctypes.windll.user32.GetThreadDpiAwarenessContext()
        for value, name in names.items():
            if ctypes.windll.user32.AreDpiAwarenessContextsEqual(
                    ctypes.c_void_p(ctx), ctypes.c_void_p(value)):
                return name
        return f"unknown({ctx})"
    except Exception as exc:
        return f"query-failed({exc})"
# -------------------------------------------------------------------------

events = queue.Queue()


def log(msg):
    """Print safely - there is no console when running as a --noconsole exe."""
    try:
        print(msg)
    except Exception:
        pass


class WindowsHotkeyListener(threading.Thread):
    """Reliable native global-hotkey message loop for the Windows build."""
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    WM_HOTKEY = 0x0312
    SNIP_ID = 1
    QUIT_ID = 2

    def __init__(self):
        super().__init__(name="OCRSnipHotkeys", daemon=True)

    def run(self):
        user32 = ctypes.windll.user32
        modifiers = self.MOD_ALT | self.MOD_CONTROL
        registered_snip = user32.RegisterHotKey(None, self.SNIP_ID, modifiers, ord("S"))
        registered_quit = user32.RegisterHotKey(None, self.QUIT_ID, modifiers, ord("Q"))
        if not registered_snip:
            events.put(("hotkey_error", "Ctrl+Alt+S is unavailable; another app may be using it."))
        if not registered_quit:
            events.put(("hotkey_error", "Ctrl+Alt+Q is unavailable; another app may be using it."))

        message = ctypes.wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message != self.WM_HOTKEY:
                    continue
                if message.wParam == self.SNIP_ID:
                    events.put("snip")
                elif message.wParam == self.QUIT_ID:
                    events.put("quit")
        finally:
            if registered_snip:
                user32.UnregisterHotKey(None, self.SNIP_ID)
            if registered_quit:
                user32.UnregisterHotKey(None, self.QUIT_ID)


# ======================= TEXT FORMATTING ==================================

def format_text(text):
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]

    if JOIN_LINES:
        text = JOIN_SEPARATOR.join(lines)
    else:
        text = "\n".join(lines)

    if CASE_MODE == "title":
        text = to_title_case(text)
    elif CASE_MODE == "sentence":
        text = to_sentence_case(text)
    elif CASE_MODE == "upper":
        text = text.upper()
    elif CASE_MODE == "lower":
        text = text.lower()

    return text


def to_title_case(text):
    def fix_word(word):
        letters = re.sub(r"[^A-Za-z]", "", word)
        if (TITLE_CASE_KEEP_SHORT_UPPER > 0
                and 0 < len(letters) <= TITLE_CASE_KEEP_SHORT_UPPER):
            return word.upper()
        return re.sub(
            r"[A-Za-z][A-Za-z']*",
            lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(),
            word,
            count=1,
        )

    return "\n".join(
        " ".join(fix_word(w) for w in ln.split(" "))
        for ln in text.splitlines()
    )


def to_sentence_case(text):
    text = text.lower()
    text = re.sub(r"([.!?]\s+)([a-z])",
                  lambda m: m.group(1) + m.group(2).upper(), text)
    text = re.sub(r"(^|\n)(\s*)([a-z])",
                  lambda m: m.group(1) + m.group(2) + m.group(3).upper(),
                  text)
    return text


# ======================= SNIP OVERLAY =====================================

def monitor_rectangles(root):
    """Return physical monitor rectangles, including monitors left of primary."""
    if sys.platform != "win32":
        return [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]

    monitors = []
    user32 = ctypes.windll.user32

    # Make sure this thread is per-monitor-v2 aware *before* enumerating, so
    # the RECTs come back in physical pixels rather than in the primary
    # monitor's virtualized (scaled) coordinate space. If awareness is wrong
    # here, external monitors with a different scale factor report a rectangle
    # that does not match their real pixels - which is exactly what leaves the
    # overlay short or shifted on those screens.
    try:
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass

    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(ctypes.wintypes.RECT), ctypes.wintypes.LPARAM,
    )

    def add_monitor(_handle, _hdc, rect, _data):
        monitors.append((rect.contents.left, rect.contents.top,
                         rect.contents.right, rect.contents.bottom))
        return 1

    try:
        user32.EnumDisplayMonitors(0, 0, callback_type(add_monitor), 0)
    except Exception as exc:
        log(f"Could not enumerate monitors: {exc}")

    if DEBUG_OVERLAY:
        for left, top, right, bottom in monitors:
            log(f"[monitor] left={left} top={top} right={right} bottom={bottom} "
                f"-> {right - left}x{bottom - top}")

    return monitors or [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]


class SnipOverlay:
    """One selection session: a single dimmed window spanning every monitor.

    Earlier versions created one Toplevel per monitor and positioned each with
    a Tk geometry string.  That breaks for any monitor placed above or left of
    the primary, because those monitors have negative screen coordinates and Tk
    geometry treats a leading minus as "measure from the opposite edge" (e.g.
    "...+126-1053" means 1053px up from the bottom, not y = -1053).  The window
    and its winfo_rootx/rooty coordinate model then disagree with the physical
    screen, so the grab area is short/offset on those monitors.

    Instead we build ONE overlay covering the whole virtual desktop, position it
    natively (SetWindowPos handles negative coordinates correctly), and do all
    selection math from absolute screen coordinates against a known origin - no
    Tk geometry offsets and no winfo_rootx involved.
    """
    def __init__(self, root, on_cancel, on_selection):
        self.root = root
        self.on_cancel = on_cancel
        self.on_selection = on_selection
        self.start = None
        self.rect = None
        self.tops = []
        self.closed = False

        rects = monitor_rectangles(root)
        self.origin_x = min(r[0] for r in rects)
        self.origin_y = min(r[1] for r in rects)
        self.width = max(r[2] for r in rects) - self.origin_x
        self.height = max(r[3] for r in rects) - self.origin_y

        window = tk.Toplevel(root)
        window.overrideredirect(True)
        # Size only here; final absolute placement (which may be negative) is
        # done natively in place_and_focus. On non-Windows the origin is (0,0).
        if sys.platform == "win32":
            window.geometry(f"{self.width}x{self.height}")
        else:
            window.geometry(
                f"{self.width}x{self.height}{self.origin_x:+d}{self.origin_y:+d}")
        window.attributes("-alpha", 0.3)
        window.attributes("-topmost", True)
        window.configure(bg="black", cursor="crosshair")

        canvas = tk.Canvas(window, bg="black", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.bind("<ButtonPress-1>", self.on_press)
        canvas.bind("<B1-Motion>", self.on_drag)
        canvas.bind("<ButtonRelease-1>", self.on_release)
        window.bind("<Escape>", lambda _event: self.cancel())

        self.window = window
        self.canvas = canvas
        self.tops.append(window)

        # Let the window manager finish mapping the window before requesting
        # focus, otherwise the hotkey can occasionally leave an inert overlay.
        self.root.after_idle(self.place_and_focus)

    def place_and_focus(self):
        """Pin the overlay to the whole virtual desktop in physical pixels."""
        if self.closed:
            return
        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            wintypes = ctypes.wintypes

            # Declare handle/argument types so the 64-bit HWNDs are passed
            # whole. Without this, ctypes marshals handles as 32-bit ints and
            # truncates them, so SetWindowPos targets a bogus window and the
            # overlay never leaves its default (0,0) position.
            user32.GetAncestor.restype = wintypes.HWND
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.SetWindowPos.restype = wintypes.BOOL
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.UINT,
            ]

            hwnd_topmost = wintypes.HWND(-1)
            swp_noactivate_show = 0x0010 | 0x0040
            ga_root = 2  # GetAncestor(GA_ROOT): the real top-level frame HWND
            self.window.update_idletasks()

            # winfo_id() can return an inner child HWND for a Tk toplevel;
            # SetWindowPos must move the actual top-level frame. SetWindowPos
            # takes raw signed coordinates, so negative origins (monitors above
            # or left of primary) are placed correctly.
            hwnd = user32.GetAncestor(self.window.winfo_id(), ga_root) or self.window.winfo_id()
            ok = user32.SetWindowPos(hwnd, hwnd_topmost,
                                     self.origin_x, self.origin_y,
                                     self.width, self.height, swp_noactivate_show)
            self.canvas.configure(width=self.width, height=self.height)
            self.window.update_idletasks()

            if DEBUG_OVERLAY:
                placed = wintypes.RECT()
                user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(placed))
                log(f"[overlay] target={self.width}x{self.height}"
                    f"@({self.origin_x},{self.origin_y}) SetWindowPos_ok={bool(ok)} "
                    f"actual=({placed.left},{placed.top})-({placed.right},{placed.bottom}) "
                    f"canvas={self.canvas.winfo_width()}x{self.canvas.winfo_height()}")
        self.focus()

    def focus(self):
        if not self.closed and self.tops:
            self.tops[0].focus_force()

    def close_windows(self):
        for window in self.tops:
            if window.winfo_exists():
                window.destroy()
        self.tops.clear()

    def cancel(self):
        if self.closed:
            return
        self.closed = True
        self.close_windows()
        self.on_cancel()

    def _to_canvas(self, x_root, y_root):
        """Map an absolute screen point to canvas coordinates."""
        return x_root - self.origin_x, y_root - self.origin_y

    def on_press(self, event):
        self.start = (event.x_root, event.y_root)
        cx, cy = self._to_canvas(event.x_root, event.y_root)
        self.rect = self.canvas.create_rectangle(
            cx, cy, cx, cy, outline="#00b7ff", width=2
        )

    def on_drag(self, event):
        if self.rect:
            x0, y0 = self._to_canvas(*self.start)
            x1, y1 = self._to_canvas(event.x_root, event.y_root)
            self.canvas.coords(self.rect, x0, y0, x1, y1)

    def on_release(self, event):
        if self.closed or not self.start:
            return
        end = (event.x_root, event.y_root)
        x1, y1 = self.start
        x2, y2 = end

        dx, dy = x2 - x1, y2 - y1
        if dx >= 0 and dy >= 0:
            rotation = None
        elif dx >= 0 and dy < 0:
            rotation = Image.ROTATE_270
        elif dx < 0 and dy >= 0:
            rotation = Image.ROTATE_90
        else:
            rotation = Image.ROTATE_180

        bbox = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

        self.closed = True
        self.close_windows()
        self.root.update()

        if bbox[2] - bbox[0] < 5 or bbox[3] - bbox[1] < 5:
            self.on_cancel()
            return

        self.root.after(150, lambda: self.on_selection(bbox, rotation))


def capture_and_ocr(root, bbox, rotation):
    try:
        if not os.path.isfile(TESSERACT_PATH):
            notify(root, "Tesseract not found",
                   f"Expected at:\n{TESSERACT_PATH}\n\n"
                   "Copy your Tesseract-OCR install into a 'tesseract' folder "
                   "next to this program.", error=True)
            return

        img = ImageGrab.grab(bbox=bbox, all_screens=True)

        if rotation is not None:
            img = img.transpose(rotation)

        w, h = img.size
        img = img.resize((w * UPSCALE, h * UPSCALE), Image.LANCZOS).convert("L")
        img = ImageOps.autocontrast(img)

        raw = pytesseract.image_to_string(img, config=TESSERACT_CONFIG).strip()

        if raw:
            text = format_text(raw)
            pyperclip.copy(text)
            log("-" * 50)
            log(text)
            log("-" * 50)
            notify(root, "Copied to clipboard", text)
        else:
            log("[no text found]")
            notify(root, "No text found",
                   "Try a tighter box or zoom in on the drawing.", error=True)
    except Exception as exc:
        log(f"[OCR error] {exc}")
        notify(root, "Could not capture text", str(exc), error=True)


# ======================= NOTIFICATION POPUP ===============================

def notify(root, title, body, error=False):
    """Snipping-tool style notification in the bottom-right corner.
    Auto-dismisses after POPUP_DURATION_MS; click anywhere to dismiss."""
    if len(body) > POPUP_MAX_CHARS:
        body = body[:POPUP_MAX_CHARS] + " ..."

    accent = "#c0392b" if error else "#00b7ff"

    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg=accent)

    # Accent border via outer frame padding
    inner = tk.Frame(win, bg="#1e1e1e")
    inner.pack(fill="both", expand=True, padx=2, pady=2)

    header = tk.Frame(inner, bg="#1e1e1e")
    header.pack(fill="x", padx=12, pady=(10, 2))
    tk.Label(header, text=("✕ " if error else "✓ ") + title,
             bg="#1e1e1e", fg=accent,
             font=("Segoe UI", 10, "bold"), anchor="w").pack(side="left")

    tk.Label(inner, text=body, bg="#1e1e1e", fg="#eeeeee",
             font=("Segoe UI", 10), justify="left", anchor="w",
             wraplength=POPUP_WIDTH - 30).pack(
        fill="x", padx=12, pady=(0, 6))

    tk.Label(inner, text="click to dismiss", bg="#1e1e1e", fg="#666666",
             font=("Segoe UI", 8), anchor="e").pack(
        fill="x", padx=12, pady=(0, 8))

    # Click anywhere on the popup to dismiss
    for widget in [win, inner, header] + list(inner.winfo_children()):
        widget.bind("<Button-1>", lambda e: win.destroy())

    # Position bottom-right, above the taskbar
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w = max(win.winfo_width(), POPUP_WIDTH)
    h = win.winfo_height()
    win.geometry(f"{w}x{h}+{sw - w - 20}+{sh - h - 70}")

    win.after(POPUP_DURATION_MS, lambda: win.winfo_exists() and win.destroy())


# ======================= MAIN LOOP ========================================

class SnipController:
    """Serializes snips so repeated hotkeys cannot stack dark overlays."""
    def __init__(self, root):
        self.root = root
        self.active = False

    def start(self):
        if self.active:
            log("[snip already active]")
            return

        self.active = True
        try:
            SnipOverlay(self.root, self.finish, self.capture)
        except Exception as exc:
            self.active = False
            log(f"[overlay error] {exc}")
            notify(self.root, "Could not start snip", str(exc), error=True)

    def capture(self, bbox, rotation):
        try:
            capture_and_ocr(self.root, bbox, rotation)
        finally:
            self.finish()

    def finish(self):
        self.active = False


def poll(root):
    try:
        while True:
            evt = events.get_nowait()
            if evt == "snip":
                root.snip_controller.start()
            elif evt == "quit":
                root.destroy()
                return
            elif isinstance(evt, tuple) and evt[0] == "hotkey_error":
                log(f"[hotkey error] {evt[1]}")
                notify(root, "Shortcut unavailable", evt[1], error=True)
    except queue.Empty:
        pass
    except Exception as exc:
        # Keep the background hotkey loop alive even if one queued action
        # fails; otherwise the process remains in Task Manager but stops
        # responding to Ctrl+Alt+S.
        log(f"[event loop error] {exc}")
    root.after(80, lambda: poll(root))


def main():
    if sys.platform == "win32":
        try:
            # A PyInstaller manifest or another library may have already set
            # process DPI awareness. Set the UI thread explicitly before Tk
            # creates any windows so monitor geometry stays in physical pixels.
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass

    root = tk.Tk()
    root.withdraw()

    if sys.platform == "win32":
        # Tk 8.6 is not per-monitor-DPI aware: it picks one scale factor at
        # startup (the primary monitor's) and applies it everywhere. Force the
        # scaling back to 1.0 so overlay geometry and canvas/mouse coordinates
        # are raw physical pixels, matching the monitor rectangles and
        # ImageGrab. Without this, external monitors on a different scale get a
        # short/offset grab area.
        try:
            root.tk.call("tk", "scaling", 1.0)
        except Exception:
            pass

    root.snip_controller = SnipController(root)

    if sys.platform == "win32":
        WindowsHotkeyListener().start()
    else:
        keyboard.add_hotkey(HOTKEY_SNIP, lambda: events.put("snip"))
        keyboard.add_hotkey(HOTKEY_QUIT, lambda: events.put("quit"))

    log(f"OCR Snip running.  {HOTKEY_SNIP.upper()} = snip,  {HOTKEY_QUIT.upper()} = quit")
    if sys.platform == "win32":
        log(f"DPI awareness: {describe_dpi_awareness()} "
            f"(runtime set: {_DPI_AWARENESS_SET})  "
            "-- must be PER_MONITOR_AWARE_V2 for correct multi-monitor overlays")
    log(f"Tesseract path: {TESSERACT_PATH}")
    log(f"Formatting: JOIN_LINES={JOIN_LINES}, CASE_MODE='{CASE_MODE}'")

    if not os.path.isfile(TESSERACT_PATH):
        notify(root, "Tesseract not found",
               f"Expected at:\n{TESSERACT_PATH}\n\n"
               "Copy your Tesseract-OCR install into a 'tesseract' folder "
               "next to this program. Snipping will fail until then.",
               error=True)

    poll(root)
    root.mainloop()


if __name__ == "__main__":
    main()
