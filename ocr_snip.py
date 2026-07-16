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
import os
import queue
import re
import sys
import tkinter as tk

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
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
# -------------------------------------------------------------------------

events = queue.Queue()


def log(msg):
    """Print safely - there is no console when running as a --noconsole exe."""
    try:
        print(msg)
    except Exception:
        pass


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

class SnipOverlay:
    def __init__(self, root):
        self.root = root
        self.start = None
        self.rect = None

        self.top = tk.Toplevel(root)
        self.top.attributes("-fullscreen", True)
        self.top.attributes("-alpha", 0.3)
        self.top.attributes("-topmost", True)
        self.top.configure(bg="black", cursor="crosshair")

        self.canvas = tk.Canvas(self.top, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.top.bind("<Escape>", lambda e: self.top.destroy())

        self.top.focus_force()

    def on_press(self, event):
        self.start = (event.x_root, event.y_root)
        self.rect = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#00b7ff", width=2, fill="white"
        )

    def on_drag(self, event):
        if self.rect:
            x0 = self.start[0] - self.top.winfo_rootx()
            y0 = self.start[1] - self.top.winfo_rooty()
            self.canvas.coords(self.rect, x0, y0, event.x, event.y)

    def on_release(self, event):
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

        self.top.destroy()
        self.root.update()

        if bbox[2] - bbox[0] < 5 or bbox[3] - bbox[1] < 5:
            return

        self.root.after(150, lambda: capture_and_ocr(self.root, bbox, rotation))


def capture_and_ocr(root, bbox, rotation):
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

def poll(root):
    try:
        while True:
            evt = events.get_nowait()
            if evt == "snip":
                SnipOverlay(root)
            elif evt == "quit":
                root.destroy()
                return
    except queue.Empty:
        pass
    root.after(80, lambda: poll(root))


def main():
    root = tk.Tk()
    root.withdraw()

    keyboard.add_hotkey(HOTKEY_SNIP, lambda: events.put("snip"))
    keyboard.add_hotkey(HOTKEY_QUIT, lambda: events.put("quit"))

    log(f"OCR Snip running.  {HOTKEY_SNIP.upper()} = snip,  {HOTKEY_QUIT.upper()} = quit")
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
