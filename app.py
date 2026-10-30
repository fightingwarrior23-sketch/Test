"""
Lecture Video Maker
PDF slides + audio + start times  ->  MP4 video (ffmpeg bundled).

- A slide can appear several times (+ button).
- Menu "Additional tools":
    * Cut from audio  - remove time ranges from the audio
    * Multiple PDFs   - several PDFs side by side, rows aligned by page number

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

# grid look
COLW, ROWH, HEADH, ROWHEADW = 200, 26, 30, 46
HEADBG, GRID, SEL, HAS, NA = "#e9e9e9", "#d0d0d0", "#0078d7", "#eaf3fc", "#f3f3f3"


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


def merge_cuts(cuts):
    merged = []
    for a, b in sorted(cuts):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def remap_time(t, cuts):
    """Time in the original audio -> time in the audio after the cuts."""
    shift = 0.0
    for a, b in cuts:
        if t >= b:
            shift += b - a
        elif t > a:
            shift += t - a
    return t - shift


def keep_segments(cuts, total):
    keep, pos = [], 0.0
    for a, b in cuts:
        if a > pos:
            keep.append((pos, a))
        pos = max(pos, b)
    if total is None or pos < total - 0.01:
        keep.append((pos, None))
    return keep


def build_cut_filter(vf, keep):
    parts = [f"[0:v]{vf}[v]"]
    for i, (s, e) in enumerate(keep):
        end = f":end={e:.3f}" if e is not None else ""
        parts.append(f"[1:a]atrim=start={s:.3f}{end},asetpts=PTS-STARTPTS[a{i}]")
    parts.append("".join(f"[a{i}]" for i in range(len(keep)))
                 + f"concat=n={len(keep)}:v=0:a=1[aout]")
    return ";".join(parts)


# -------------------------------------------------------------------- app
class App:
    def __init__(self, root):
        self.root = root
        root.title("Lecture Video Maker")
        root.geometry("1120x720")
        self.pdf = tk.StringVar()
        self.audio = tk.StringVar()
        self.out = tk.StringVar()
        self.fps = tk.StringVar(value="2")
        self.crf = tk.StringVar(value="30")
        self.multi = tk.BooleanVar(value=False)
        self.cut_on = tk.BooleanVar(value=False)

        self.docs = []       # [{"path": str, "doc": fitz.Document}]
        self.times = {}      # (doc index, page index) -> sorted list of seconds
        self.cur = None      # selected (doc index, page index)
        self.rows = []       # time Entry widgets of the selected cell
        self.cuts = []       # [(frame, from_entry, to_entry)]
        self.thumb = None
        self.busy = False

        self.build_menu()
        self.build_top()
        self.build_cut_panel()
        self.build_main()
        self.build_bottom()

    # ------------------------------------------------------------ layout
    def build_menu(self):
        bar = tk.Menu(self.root)
        tools = tk.Menu(bar, tearoff=0)
        tools.add_checkbutton(label="Cut from audio", variable=self.cut_on,
                              command=self.toggle_cut)
        tools.add_checkbutton(label="Multiple PDFs", variable=self.multi,
                              command=self.toggle_multi)
        bar.add_cascade(label="Additional tools", menu=tools)
        self.root.config(menu=bar)

    def build_top(self):
        self.top = ttk.Frame(self.root, padding=8)
        self.top.pack(fill="x")
        rows = [("PDF", self.pdf, self.pick_pdf),
                ("Audio", self.audio, self.pick_audio),
                ("Output", self.out, self.pick_out)]
        for r, (label, var, cmd) in enumerate(rows):
            lab = ttk.Label(self.top, text=label, width=7)
            lab.grid(row=r, column=0, sticky="w")
            ent = ttk.Entry(self.top, textvariable=var)
            ent.grid(row=r, column=1, sticky="ew", padx=4, pady=2)
            ttk.Button(self.top, text="Browse…", command=cmd).grid(row=r, column=2)
            if r == 0:
                self.pdf_label = lab
                ent.configure(state="readonly")
        self.top.columnconfigure(1, weight=1)

    def build_cut_panel(self):
        self.cut_frame = ttk.LabelFrame(
            self.root, padding=6,
            text="Cut from audio: these parts are removed (times = original audio)")
        self.cut_rows = ttk.Frame(self.cut_frame)
        self.cut_rows.pack(anchor="w")
        ttk.Button(self.cut_frame, text="+  Add a cut",
                   command=self.add_cut).pack(anchor="w", pady=(4, 0))

    def build_main(self):
        mid = ttk.Frame(self.root, padding=8)
        mid.pack(fill="both", expand=True)

        # right: preview + editor
        right = ttk.Frame(mid, padding=(12, 0))
        right.pack(side="right", fill="y")
        tk.Frame(right, width=500, height=1).pack(side="top")

        self.edit_box = ttk.LabelFrame(right, text="Select a page", padding=6)
        self.edit_box.pack(side="bottom", fill="x", pady=6)
        self.rows_frame = ttk.Frame(self.edit_box)
        self.rows_frame.pack(anchor="w")
        btns = ttk.Frame(self.edit_box)
        btns.pack(anchor="w", pady=(4, 0))
        ttk.Button(btns, text="+  Add another time",
                   command=lambda: self.add_row(focus=True)).pack(side="left")
        ttk.Button(btns, text="Save & next ↵", command=self.set_time).pack(side="left", padx=8)
        ttk.Label(self.edit_box, text="Leave empty to skip this page.").pack(anchor="w", pady=(4, 0))

        opts = ttk.Frame(right)
        opts.pack(side="bottom", fill="x")
        ttk.Label(opts, text="FPS").pack(side="left")
        ttk.Entry(opts, textvariable=self.fps, width=4).pack(side="left", padx=(2, 12))
        ttk.Label(opts, text="CRF (lower = better quality)").pack(side="left")
        ttk.Entry(opts, textvariable=self.crf, width=4).pack(side="left", padx=2)

        self.preview = ttk.Label(right, anchor="center")
        self.preview.pack(fill="both", expand=True)

        # left: grid (one column per PDF, one row per page number)
        left = ttk.Frame(mid)
        left.pack(side="left", fill="both", expand=True)
        left.rowconfigure(1, weight=1)
        left.columnconfigure(1, weight=1)

        corner = tk.Canvas(left, width=ROWHEADW, height=HEADH, bg=HEADBG, highlightthickness=0)
        corner.grid(row=0, column=0)
        corner.create_rectangle(0, 0, ROWHEADW, HEADH, fill=HEADBG, outline=GRID)
        corner.create_text(ROWHEADW / 2, HEADH / 2, text="Page")

        self.head = tk.Canvas(left, height=HEADH, bg=HEADBG, highlightthickness=0,
                              xscrollincrement=40)
        self.head.grid(row=0, column=1, sticky="ew")
        self.rowhead = tk.Canvas(left, width=ROWHEADW, bg=HEADBG, highlightthickness=0,
                                 yscrollincrement=ROWH)
        self.rowhead.grid(row=1, column=0, sticky="ns")
        self.body = tk.Canvas(left, bg="white", highlightthickness=0,
                              xscrollincrement=40, yscrollincrement=ROWH)
        self.body.grid(row=1, column=1, sticky="nsew")
        self.vbar = ttk.Scrollbar(left, orient="vertical", command=self.yview)
        self.vbar.grid(row=1, column=2, sticky="ns")
        self.hbar = ttk.Scrollbar(left, orient="horizontal", command=self.xview)
        self.hbar.grid(row=2, column=1, sticky="ew")
        self.body.configure(xscrollcommand=self.hbar.set, yscrollcommand=self.vbar.set)

        self.body.bind("<Button-1>", self.on_body_click)
        self.head.bind("<Button-1>", self.on_head_click)
        for w in (self.body, self.head, self.rowhead):
            w.bind("<MouseWheel>", self.on_wheel)

    def build_bottom(self):
        bottom = ttk.Frame(self.root, padding=8)
        bottom.pack(fill="x")
        self.bar = ttk.Progressbar(bottom, maximum=100)
        self.bar.pack(fill="x", side="left", expand=True, padx=(0, 8))
        self.status = ttk.Label(bottom, text="Pick a PDF to start", width=30)
        self.status.pack(side="left")
        self.go = ttk.Button(bottom, text="Make video", command=self.start)
        self.go.pack(side="left", padx=(8, 0))

    # ------------------------------------------------------ scrolling
    def xview(self, *args):
        self.body.xview(*args)
        self.head.xview(*args)

    def yview(self, *args):
        self.body.yview(*args)
        self.rowhead.yview(*args)

    def on_wheel(self, e):
        n = -3 if e.delta > 0 else 3
        if e.state & 0x1:  # shift = sideways
            self.xview("scroll", n, "units")
        else:
            self.yview("scroll", n // 3, "units")

    def ensure_visible(self, c, r):
        W, H = len(self.docs) * COLW, self.page_rows() * ROWH
        if W <= 0 or H <= 0:
            return
        x0, y0 = c * COLW, r * ROWH
        vx, vy = self.body.canvasx(0), self.body.canvasy(0)
        vw, vh = self.body.winfo_width(), self.body.winfo_height()
        if vw > 1:
            if x0 < vx:
                self.xview("moveto", x0 / W)
            elif x0 + COLW > vx + vw:
                self.xview("moveto", (x0 + COLW - vw) / W)
        if vh > 1:
            if y0 < vy:
                self.yview("moveto", y0 / H)
            elif y0 + ROWH > vy + vh:
                self.yview("moveto", (y0 + ROWH - vh) / H)

    # ------------------------------------------------------------ grid
    def page_rows(self):
        return max((len(d["doc"]) for d in self.docs), default=0)

    def cell_text(self, c, r):
        s = ",  ".join(fmt_time(t) for t in self.times.get((c, r), []))
        return s if len(s) <= 26 else s[:25] + "…"

    def draw_cell(self, c, r):
        if c >= len(self.docs):
            return
        tag = f"c{c}_{r}"
        self.body.delete(tag)
        x, y = c * COLW, r * ROWH
        exists = r < len(self.docs[c]["doc"])
        sel = self.cur == (c, r)
        has = (c, r) in self.times
        fill = SEL if sel else (HAS if has else ("white" if exists else NA))
        self.body.create_rectangle(x, y, x + COLW, y + ROWH, fill=fill, outline=GRID, tags=tag)
        if exists:
            self.body.create_text(x + 10, y + ROWH / 2, text=self.cell_text(c, r), anchor="w",
                                  fill="white" if sel else "black", tags=tag)

    def redraw_grid(self):
        nd, nr = len(self.docs), self.page_rows()
        W, H = max(nd * COLW, 1), max(nr * ROWH, 1)
        for cv in (self.head, self.rowhead, self.body):
            cv.delete("all")
        self.body.configure(scrollregion=(0, 0, W, H))
        self.head.configure(scrollregion=(0, 0, W, HEADH))
        self.rowhead.configure(scrollregion=(0, 0, ROWHEADW, H))
        maxc = 24 if self.multi.get() else 28
        for i, d in enumerate(self.docs):
            x = i * COLW
            name = os.path.basename(d["path"])
            if self.multi.get():
                name = f"{i + 1}. {name}"
            if len(name) > maxc:
                name = name[:maxc - 1] + "…"
            self.head.create_rectangle(x, 0, x + COLW, HEADH, fill=HEADBG, outline=GRID)
            self.head.create_text(x + 8, HEADH / 2, text=name, anchor="w")
            if self.multi.get():
                self.head.create_text(x + COLW - 12, HEADH / 2, text="×", fill="#b00020",
                                      font=("TkDefaultFont", 12, "bold"))
        for r in range(nr):
            self.rowhead.create_rectangle(0, r * ROWH, ROWHEADW, (r + 1) * ROWH,
                                          fill=HEADBG, outline=GRID)
            self.rowhead.create_text(ROWHEADW / 2, r * ROWH + ROWH / 2, text=str(r + 1))
        for r in range(nr):
            for c in range(nd):
                self.draw_cell(c, r)

    def on_body_click(self, e):
        self.select_cell(int(self.body.canvasx(e.x) // COLW), int(self.body.canvasy(e.y) // ROWH))

    def on_head_click(self, e):
        x = self.head.canvasx(e.x)
        c = int(x // COLW)
        if self.multi.get() and c < len(self.docs) and x - c * COLW > COLW - 24:
            self.remove_doc(c)

    # ---------------------------------------------------------- pickers
    def refresh_pdf_label(self):
        if self.multi.get():
            self.pdf_label.config(text="PDFs")
            self.pdf.set(";  ".join(os.path.basename(d["path"]) for d in self.docs))
        else:
            self.pdf_label.config(text="PDF")
            self.pdf.set(self.docs[0]["path"] if self.docs else "")
        pages = sum(len(d["doc"]) for d in self.docs)
        if self.docs:
            self.status.config(text=f"{len(self.docs)} PDF(s), {pages} pages"
                               if self.multi.get() else f"{pages} pages loaded")

    def pick_pdf(self):
        if self.busy:
            return
        if self.multi.get():
            ps = filedialog.askopenfilenames(filetypes=[("PDF", "*.pdf")])
            if ps:
                self.add_pdfs(list(ps), replace=False)
        else:
            p = filedialog.askopenfilename(filetypes=[("PDF", "*.pdf")])
            if p:
                self.add_pdfs([p], replace=True)

    def add_pdfs(self, paths, replace):
        new = []
        for p in paths:
            try:
                new.append({"path": p, "doc": fitz.open(p)})
            except Exception as e:
                messagebox.showerror("Error", f"Can't open PDF:\n{p}\n{e}")
        if not new:
            return
        if replace:
            for d in self.docs:
                d["doc"].close()
            self.docs, self.times, self.cur = [], {}, None
        elif self.cur:
            self.commit(self.cur, False)
        first = len(self.docs)
        self.docs += new
        if first == 0:
            self.times[(0, 0)] = [0.0]
        if not self.out.get():
            self.out.set(os.path.splitext(self.docs[0]["path"])[0] + ".mp4")
        self.refresh_pdf_label()
        self.redraw_grid()
        self.select_cell(first, 0)

    def remove_doc(self, c):
        if self.busy:
            return
        if any(k[0] == c for k in self.times):
            name = os.path.basename(self.docs[c]["path"])
            if not messagebox.askyesno("Remove PDF", f"Remove {name} and its times?"):
                return
        if self.cur and self.cur[0] != c:
            self.commit(self.cur, False)
        self.docs[c]["doc"].close()
        del self.docs[c]
        self.times = {((d - 1) if d > c else d, p): v
                      for (d, p), v in self.times.items() if d != c}
        if self.cur:
            if self.cur[0] == c:
                self.cur = None
            elif self.cur[0] > c:
                self.cur = (self.cur[0] - 1, self.cur[1])
        self.refresh_pdf_label()
        if not self.docs:
            self.pdf.set("")
            self.status.config(text="Pick a PDF to start")
        self.redraw_grid()
        if self.docs and self.cur is None:
            self.select_cell(min(c, len(self.docs) - 1), 0)
        elif not self.docs:
            self.preview.config(image="")
            self.thumb = None
            self.edit_box.config(text="Select a page")
            self.build_rows(None)

    def pick_audio(self):
        p = filedialog.askopenfilename(
            filetypes=[("Audio", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac *.wma"), ("All", "*.*")])
        if p:
            self.audio.set(p)

    def pick_out(self):
        p = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4", "*.mp4")])
        if p:
            self.out.set(p)

    # ------------------------------------------------ additional tools
    def toggle_multi(self):
        if not self.multi.get() and len(self.docs) > 1:
            if not messagebox.askyesno(
                    "Multiple PDFs",
                    "Turning this off keeps only the first PDF and removes the others "
                    "(and their times). Continue?"):
                self.multi.set(True)
                return
            while len(self.docs) > 1:
                self.remove_doc_silent(len(self.docs) - 1)
        self.refresh_pdf_label()
        self.redraw_grid()

    def remove_doc_silent(self, c):
        self.docs[c]["doc"].close()
        del self.docs[c]
        self.times = {k: v for k, v in self.times.items() if k[0] != c}
        if self.cur and self.cur[0] == c:
            self.cur = None
        if self.cur is None and self.docs:
            self.cur = None
            self.select_cell(0, 0)

    def toggle_cut(self):
        if self.cut_on.get():
            self.cut_frame.pack(after=self.top, fill="x", padx=8)
            if not self.cuts:
                self.add_cut()
        else:
            self.cut_frame.pack_forget()

    def add_cut(self):
        f = ttk.Frame(self.cut_rows)
        ttk.Label(f, text="from").pack(side="left")
        ea = ttk.Entry(f, width=9)
        ea.pack(side="left", padx=3)
        ttk.Label(f, text="to").pack(side="left")
        eb = ttk.Entry(f, width=9)
        eb.pack(side="left", padx=3)
        ttk.Button(f, text="×", width=2, command=lambda: self.del_cut(f)).pack(side="left", padx=3)
        self.cuts.append((f, ea, eb))
        self.regrid_cuts()
        ea.focus_set()

    def del_cut(self, frame):
        self.cuts = [c for c in self.cuts if c[0] is not frame]
        frame.destroy()
        self.regrid_cuts()

    def regrid_cuts(self):
        for i, (f, _, _) in enumerate(self.cuts):
            f.grid(row=i // 3, column=i % 3, padx=8, pady=2, sticky="w")

    def read_cuts(self):
        out = []
        for _, ea, eb in self.cuts:
            try:
                a, b = parse_time(ea.get()), parse_time(eb.get())
            except ValueError:
                raise ValueError("A cut has a time I can't read. Use m:ss or h:mm:ss.")
            if a is None and b is None:
                continue
            if a is None or b is None:
                raise ValueError("Fill both 'from' and 'to' in every cut (or clear that row).")
            if b <= a:
                raise ValueError(f"Cut {fmt_time(a)} to {fmt_time(b)}: 'to' must be after 'from'.")
            out.append((a, b))
        return merge_cuts(out)

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

    def commit(self, key, loud):
        """Store the entries' times for `key` = (pdf, page). False on bad input."""
        try:
            ts = sorted({parse_time(e.get()) for e in self.rows} - {None})
        except ValueError:
            if loud:
                messagebox.showerror("Bad time", "Use m:ss, h:mm:ss or plain seconds.")
            return False
        if ts:
            self.times[key] = ts
        else:
            self.times.pop(key, None)
        self.draw_cell(*key)
        return True

    # --------------------------------------------------- selecting cells
    def select_cell(self, c, r):
        if self.busy or c < 0 or r < 0 or c >= len(self.docs) or r >= len(self.docs[c]["doc"]):
            return
        if self.cur == (c, r):
            return
        old = self.cur
        if old and old[0] < len(self.docs):
            self.commit(old, False)
        self.cur = (c, r)
        if old and old[0] < len(self.docs):
            self.draw_cell(*old)
        self.draw_cell(c, r)

        page = self.docs[c]["doc"][r]
        rect = page.rect
        z = min(480 / rect.width, 270 / rect.height)
        png = page.get_pixmap(matrix=fitz.Matrix(z, z)).tobytes("png")
        self.thumb = tk.PhotoImage(data=base64.b64encode(png))
        self.preview.config(image=self.thumb)

        who = f"PDF {c + 1} · page {r + 1}" if self.multi.get() else f"Page {r + 1}"
        self.edit_box.config(text=f"{who} appears at (m:ss or h:mm:ss)")
        self.build_rows(self.times.get((c, r)))
        self.ensure_visible(c, r)

    def set_time(self, _=None):
        if self.cur is None or self.busy:
            return
        c, r = self.cur
        if not self.commit(self.cur, True):
            return
        if r + 1 < len(self.docs[c]["doc"]):  # next page of the same PDF
            self.select_cell(c, r + 1)

    # ----------------------------------------------------------- export
    def name_of(self, d, p):
        return f"PDF {d + 1} page {p + 1}" if self.multi.get() else f"Page {p + 1}"

    def start(self):
        if self.busy:
            return
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            messagebox.showerror("ffmpeg missing", "ffmpeg.exe not found (should be bundled).")
            return
        if not self.docs or not self.audio.get() or not self.out.get():
            messagebox.showwarning("Missing", "Pick a PDF, an audio file and an output file.")
            return
        if self.cur is not None and not self.commit(self.cur, loud=True):
            return
        cues = sorted((t, d, p) for (d, p), ts in self.times.items() for t in ts)
        if not cues:
            messagebox.showwarning("Missing", "Set at least one start time.")
            return
        for (a, da, pa), (b, db, pb) in zip(cues, cues[1:]):
            if b <= a:
                messagebox.showerror(
                    "Same time",
                    f"{self.name_of(da, pa)} and {self.name_of(db, pb)} "
                    f"both start at {fmt_time(a)}.")
                return
        cuts = []
        if self.cut_on.get():
            try:
                cuts = self.read_cuts()
            except ValueError as e:
                messagebox.showerror("Cut from audio", str(e))
                return
        try:
            fps = int(self.fps.get())
            crf = int(self.crf.get())
        except ValueError:
            messagebox.showerror("Options", "FPS and CRF must be whole numbers.")
            return
        self.busy = True
        self.go.config(state="disabled")
        threading.Thread(target=self.work, args=(ffmpeg, cues, cuts, fps, crf),
                         daemon=True).start()

    def ui(self, text=None, pct=None, done=None):
        def f():
            if text is not None:
                self.status.config(text=text)
            if pct is not None:
                self.bar["value"] = pct
            if done is not None:
                self.busy = False
                self.go.config(state="normal")
                (messagebox.showinfo if done[0] else messagebox.showerror)(
                    "Done" if done[0] else "Error", done[1])
        self.root.after(0, f)

    def work(self, ffmpeg, cues, cuts, fps, crf):
        tmp = tempfile.mkdtemp(prefix="lecvid_")
        try:
            audio = self.audio.get()
            total = audio_duration(ffmpeg, audio)

            # cuts: clip to the audio, move every start time to the shortened audio
            if total:
                cuts = [(a, min(b, total)) for a, b in cuts if a < total]
            length = total
            if cuts:
                if total:
                    length = total - sum(b - a for a, b in cuts)
                    if length < 0.5:
                        raise ValueError("Nothing is left of the audio after the cuts.")
                mapped = {}
                for t, d, p in cues:  # a later slide wins if two land on the same time
                    mapped[round(remap_time(t, cuts), 3)] = (d, p)
                cues = [(t, d, p) for t, (d, p) in sorted(mapped.items())]

            # 1) extract each used page once, as an HD image
            paths = {}
            for d, p in sorted({(d, p) for _, d, p in cues}):
                self.ui(f"Rendering {self.name_of(d, p)}…", 0)
                paths[(d, p)] = os.path.join(tmp, f"{d + 1}_{p + 1}.png")
                render_page(self.docs[d]["doc"], p, paths[(d, p)])

            # 2) build concat list (first cue always starts at 0)
            files = [paths[(d, p)] for _, d, p in cues]
            starts = [0.0] + [t for t, _, _ in cues[1:]]
            durs = [b - a for a, b in zip(starts, starts[1:])]
            durs.append(max(length - starts[-1], 1) if length else 36000)

            lst = os.path.join(tmp, "list.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for p_, d_ in zip(files, durs):
                    f.write(f"file '{p_.replace(os.sep, '/')}'\nduration {d_:.3f}\n")
                f.write(f"file '{files[-1].replace(os.sep, '/')}'\n")  # concat quirk

            # 3) ffmpeg
            vf = (f"fps={fps},scale=1920:1080:force_original_aspect_ratio=decrease,"
                  "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p")
            if cuts:
                keep = keep_segments(cuts, total)
                if not keep:
                    raise ValueError("Nothing is left of the audio after the cuts.")
                maps = ["-filter_complex", build_cut_filter(vf, keep),
                        "-map", "[v]", "-map", "[aout]"]
            else:
                maps = ["-vf", vf]
            cmd = ([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", lst, "-i", audio] + maps +
                   ["-c:v", "libx264", "-tune", "stillimage", "-crf", str(crf),
                    "-c:a", "aac", "-b:a", "96k", "-shortest",
                    "-progress", "pipe:1", "-nostats", self.out.get()])
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", creationflags=NOWIN)
            tail = []
            for line in proc.stdout:
                tail = (tail + [line.rstrip()])[-25:]
                m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
                if m and length:
                    sec = int(m.group(1)) / 1e6
                    self.ui(f"Encoding {fmt_time(sec)} / {fmt_time(length)}",
                            min(sec / length * 100, 100))
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
