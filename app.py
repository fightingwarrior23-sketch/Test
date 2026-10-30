"""
Lecture Video Maker
PDF slides + audio + start times  ->  MP4 video (ffmpeg bundled).
A slide can appear several times (use the + button).

Requires: pip install pymupdf
"""
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import fitz  # PyMuPDF

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # hide ffmpeg console on Windows


# ---------------------------------------------------------------- helpers
def base_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def find_ffmpeg():
    local = os.path.join(base_dir(), "ffmpeg.exe")
    if os.path.exists(local):
        return local
    return shutil.which("ffmpeg")


def parse_time(text):
    """'90' -> 90s, '1:30' -> 90s, '1:02:03' -> 3723s. Returns None if empty."""
    text = text.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(text)
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def fmt_time(sec):
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def audio_duration(ffmpeg, audio):
    p = subprocess.run([ffmpeg, "-i", audio], capture_output=True, text=True,
                       errors="replace", creationflags=NOWIN)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def render_page(doc, idx, path, width=1920, height=1080):
    r = doc[idx].rect
    zoom = min(width / r.width, height / r.height)
    doc[idx].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).save(path)


# -------------------------------------------------------------------- app
class App:
    def __init__(self, root):
        self.root = root
        root.title("Lecture Video Maker")
        root.geometry("920x640")
        self.pdf = tk.StringVar()
        self.audio = tk.StringVar()
        self.out = tk.StringVar()
        self.fps = tk.StringVar(value="2")
        self.crf = tk.StringVar(value="30")
        self.doc = None
        self.times = {}      # page index -> sorted list of start times (seconds)
        self.current = None  # page index shown in the editor
        self.rows = []       # time Entry widgets for the current page
        self.thumb = None

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        for r, (label, var, cmd) in enumerate([
            ("PDF", self.pdf, self.pick_pdf),
            ("Audio", self.audio, self.pick_audio),
            ("Output", self.out, self.pick_out),
        ]):
            ttk.Label(top, text=label, width=7).grid(row=r, column=0, sticky="w")
            ttk.Entry(top, textvariable=var).grid(row=r, column=1, sticky="ew", padx=4, pady=2)
            ttk.Button(top, text="Browse…", command=cmd).grid(row=r, column=2)
        top.columnconfigure(1, weight=1)

        mid = ttk.Frame(root, padding=8)
        mid.pack(fill="both", expand=True)

        left = ttk.Frame(mid)
        left.pack(side="left", fill="y")
        self.tree = ttk.Treeview(left, columns=("page", "start"), show="headings",
                                 height=18, selectmode="browse")
        self.tree.heading("page", text="Page")
        self.tree.heading("start", text="Appears at")
        self.tree.column("page", width=60, anchor="center")
        self.tree.column("start", width=190, anchor="w")
        sb = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="y")
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        right = ttk.Frame(mid, padding=(12, 0))
        right.pack(side="left", fill="both", expand=True)

        edit = ttk.LabelFrame(right, text="This slide appears at (m:ss or h:mm:ss)", padding=6)
        edit.pack(side="bottom", fill="x", pady=6)
        self.rows_frame = ttk.Frame(edit)
        self.rows_frame.pack(anchor="w")
        btns = ttk.Frame(edit)
        btns.pack(anchor="w", pady=(4, 0))
        ttk.Button(btns, text="+  Add another time", command=lambda: self.add_row(focus=True)).pack(side="left")
        ttk.Button(btns, text="Save & next ↵", command=self.set_time).pack(side="left", padx=8)
        ttk.Label(edit, text="Leave empty to skip this page.").pack(anchor="w", pady=(4, 0))

        opts = ttk.Frame(right)
        opts.pack(side="bottom", fill="x")
        ttk.Label(opts, text="FPS").pack(side="left")
        ttk.Entry(opts, textvariable=self.fps, width=4).pack(side="left", padx=(2, 12))
        ttk.Label(opts, text="CRF (lower = better quality)").pack(side="left")
        ttk.Entry(opts, textvariable=self.crf, width=4).pack(side="left", padx=2)

        self.preview = ttk.Label(right, anchor="center")
        self.preview.pack(fill="both", expand=True)

        bottom = ttk.Frame(root, padding=8)
        bottom.pack(fill="x")
        self.bar = ttk.Progressbar(bottom, maximum=100)
        self.bar.pack(fill="x", side="left", expand=True, padx=(0, 8))
        self.status = ttk.Label(bottom, text="Pick a PDF to start", width=30)
        self.status.pack(side="left")
        self.go = ttk.Button(bottom, text="Make video", command=self.start)
        self.go.pack(side="left", padx=(8, 0))

    # ---------------------------------------------------------- pickers
    def pick_pdf(self):
        p = filedialog.askopenfilename(filetypes=[("PDF", "*.pdf")])
        if not p:
            return
        try:
            self.doc = fitz.open(p)
        except Exception as e:
            messagebox.showerror("Error", f"Can't open PDF:\n{e}")
            return
        self.pdf.set(p)
        if not self.out.get():
            self.out.set(os.path.splitext(p)[0] + ".mp4")
        self.current = None
        self.times = {0: [0.0]}
        self.tree.delete(*self.tree.get_children())
        for i in range(len(self.doc)):
            self.tree.insert("", "end", iid=str(i), values=(i + 1, ""))
        self.refresh_row(0)
        self.tree.selection_set("0")
        self.status.config(text=f"{len(self.doc)} pages loaded")

    def pick_audio(self):
        p = filedialog.askopenfilename(
            filetypes=[("Audio", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac *.wma"), ("All", "*.*")])
        if p:
            self.audio.set(p)

    def pick_out(self):
        p = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4", "*.mp4")])
        if p:
            self.out.set(p)

    # ------------------------------------------------- time entry rows
    def add_row(self, text="", focus=False):
        f = ttk.Frame(self.rows_frame)
        f.pack(anchor="w", pady=1)
        e = ttk.Entry(f, width=11)
        e.insert(0, text)
        e.pack(side="left")
        e.bind("<Return>", self.set_time)
        ttk.Button(f, text="×", width=2, command=lambda: self.del_row(f, e)).pack(side="left", padx=3)
        self.rows.append(e)
        if focus:
            e.focus_set()

    def del_row(self, frame, entry):
        self.rows.remove(entry)
        frame.destroy()
        if not self.rows:
            self.add_row()

    def build_rows(self, times):
        for w in self.rows_frame.winfo_children():
            w.destroy()
        self.rows = []
        for t in (times or [None]):
            self.add_row("" if t is None else fmt_time(t))
        self.rows[0].focus_set()
        self.rows[0].select_range(0, "end")

    def commit(self, page, loud):
        """Store the entries' times for `page`. Returns False on bad input."""
        try:
            ts = sorted({parse_time(e.get()) for e in self.rows} - {None})
        except ValueError:
            if loud:
                messagebox.showerror("Bad time", "Use m:ss, h:mm:ss or plain seconds.")
            return False
        if ts:
            self.times[page] = ts
        else:
            self.times.pop(page, None)
        self.refresh_row(page)
        return True

    # ------------------------------------------------------ slide table
    def refresh_row(self, i):
        ts = self.times.get(i, [])
        self.tree.item(str(i), values=(i + 1, ",  ".join(fmt_time(t) for t in ts)))

    def on_select(self, _=None):
        sel = self.tree.selection()
        if not sel or not self.doc:
            return
        new = int(sel[0])
        if self.current is not None and self.current != new:
            self.commit(self.current, loud=False)
        self.current = new
        r = self.doc[new].rect
        z = min(480 / r.width, 270 / r.height)
        png = self.doc[new].get_pixmap(matrix=fitz.Matrix(z, z)).tobytes("png")
        self.thumb = tk.PhotoImage(data=base64.b64encode(png))
        self.preview.config(image=self.thumb)
        self.build_rows(self.times.get(new))

    def set_time(self, _=None):
        if self.current is None:
            return
        i = self.current
        if not self.commit(i, loud=True):
            return
        if i + 1 < len(self.doc):  # jump to next page for fast entry
            self.tree.selection_set(str(i + 1))
            self.tree.see(str(i + 1))

    # ----------------------------------------------------------- export
    def start(self):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            messagebox.showerror("ffmpeg missing", "ffmpeg.exe not found (should be bundled).")
            return
        if not self.doc or not self.audio.get() or not self.out.get():
            messagebox.showwarning("Missing", "Pick a PDF, an audio file and an output file.")
            return
        if self.current is not None and not self.commit(self.current, loud=True):
            return
        cues = sorted((t, idx) for idx, ts in self.times.items() for t in ts)
        if not cues:
            messagebox.showwarning("Missing", "Set at least one start time.")
            return
        for (a, pa), (b, pb) in zip(cues, cues[1:]):
            if b <= a:
                messagebox.showerror(
                    "Same time",
                    f"Page {pa + 1} and page {pb + 1} both start at {fmt_time(a)}.")
                return
        try:
            fps = int(self.fps.get())
            crf = int(self.crf.get())
        except ValueError:
            messagebox.showerror("Options", "FPS and CRF must be whole numbers.")
            return
        self.go.config(state="disabled")
        threading.Thread(target=self.work, args=(ffmpeg, cues, fps, crf), daemon=True).start()

    def ui(self, text=None, pct=None, done=None):
        def f():
            if text is not None:
                self.status.config(text=text)
            if pct is not None:
                self.bar["value"] = pct
            if done is not None:
                self.go.config(state="normal")
                (messagebox.showinfo if done[0] else messagebox.showerror)(
                    "Done" if done[0] else "Error", done[1])
        self.root.after(0, f)

    def work(self, ffmpeg, cues, fps, crf):
        tmp = tempfile.mkdtemp(prefix="lecvid_")
        try:
            audio = self.audio.get()
            total = audio_duration(ffmpeg, audio)

            # 1) extract each used page once, as an HD image
            paths = {}
            for idx in sorted({p for _, p in cues}):
                self.ui(f"Rendering page {idx + 1}…", 0)
                paths[idx] = os.path.join(tmp, f"{idx + 1}.png")
                render_page(self.doc, idx, paths[idx])

            # 2) build concat list (first cue always starts at 0)
            files = [paths[idx] for _, idx in cues]
            starts = [0.0] + [t for t, _ in cues[1:]]
            durs = [b - a for a, b in zip(starts, starts[1:])]
            durs.append(max(total - starts[-1], 1) if total else 36000)

            lst = os.path.join(tmp, "list.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for p, d in zip(files, durs):
                    f.write(f"file '{p.replace(os.sep, '/')}'\nduration {d:.3f}\n")
                f.write(f"file '{files[-1].replace(os.sep, '/')}'\n")  # concat quirk

            # 3) ffmpeg
            vf = (f"fps={fps},scale=1920:1080:force_original_aspect_ratio=decrease,"
                  "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p")
            cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-i", audio,
                   "-vf", vf, "-c:v", "libx264", "-tune", "stillimage", "-crf", str(crf),
                   "-c:a", "aac", "-b:a", "96k", "-shortest",
                   "-progress", "pipe:1", "-nostats", self.out.get()]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", creationflags=NOWIN)
            tail = []
            for line in proc.stdout:
                tail = (tail + [line.rstrip()])[-25:]
                m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
                if m and total:
                    sec = int(m.group(1)) / 1e6
                    self.ui(f"Encoding {fmt_time(sec)} / {fmt_time(total)}",
                            min(sec / total * 100, 100))
            proc.wait()
            if proc.returncode == 0:
                self.ui("Finished", 100, (True, f"Saved:\n{self.out.get()}"))
            else:
                self.ui("Failed", None, (False, "ffmpeg failed:\n" + "\n".join(tail)))
        except Exception as e:
            self.ui("Failed", None, (False, str(e)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
