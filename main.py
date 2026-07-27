"""MetaCompare — compare the metadata of two files side by side.

Drag and drop two files (or browse for them) and get a readout of which
metadata properties match and which differ. Covers file-system metadata
(size, timestamps, attributes, hashes) and embedded metadata (EXIF, ID3,
video/document properties) for any format hachoir can parse.
"""

import hashlib
import mimetypes
import os
import queue
import stat
import sys
import threading
import tkinter as tk
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

try:
    from hachoir.core import config as _hachoir_config
    from hachoir.core.log import log as _hachoir_log
    from hachoir.metadata import extractMetadata
    from hachoir.parser import createParser

    _hachoir_config.quiet = True
    # hachoir writes error-level messages straight to sys.stderr and flushes
    # sys.stdout, both of which are None in a --windowed PyInstaller build.
    # The resulting AttributeError would abort extraction and silently cost us
    # all embedded metadata, so keep hachoir away from stdio entirely.
    _hachoir_log.use_print = False
    HACHOIR_AVAILABLE = True
except ImportError:
    HACHOIR_AVAILABLE = False

APP_NAME = "MetaCompare"
HASH_CHUNK = 1024 * 1024
MAX_CELL_LEN = 160
POLL_INTERVAL_MS = 80
KEY_SEPARATOR = " — "

STATUS_SAME = "Same"
STATUS_DIFF = "Different"
STATUS_ONLY_1 = "Only file 1"
STATUS_ONLY_2 = "Only file 2"

# hachoir titles a file's ungrouped metadata "Metadata" for simple formats and
# "Common" once the file has stream groups. Both mean "not in a group", and
# treating them differently would stop a PNG's "MIME type" from lining up with
# a WAV's.
ROOT_SECTION_HEADERS = {"metadata", "common"}


# --------------------------------------------------------------------------
# Metadata extraction
# --------------------------------------------------------------------------

def human_size(num_bytes):
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(n):,} {unit}"
            return f"{n:,.2f} {unit}"
        n /= 1024


def _fmt_time(timestamp):
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return str(timestamp)


def _file_hashes(path):
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(HASH_CHUNK)
            if not chunk:
                break
            md5.update(chunk)
            sha256.update(chunk)
    return md5.hexdigest(), sha256.hexdigest()


def is_digest(value):
    """True if *value* is a real hex digest rather than an error placeholder."""
    return bool(value) and all(c in "0123456789abcdef" for c in value)


def filesystem_metadata(path):
    """Return an OrderedDict of file-system level properties for *path*."""
    path = Path(path)
    st = path.stat()
    props = OrderedDict()
    props["Name"] = path.name
    props["Extension"] = path.suffix.lower() or "(none)"
    mime, encoding = mimetypes.guess_type(path.name)
    props["MIME type (by extension)"] = mime or "unknown"
    if encoding:
        props["Encoding (by extension)"] = encoding
    props["Size"] = f"{human_size(st.st_size)} ({st.st_size:,} bytes)"
    props["Created"] = _fmt_time(st.st_ctime)
    props["Modified"] = _fmt_time(st.st_mtime)
    props["Accessed"] = _fmt_time(st.st_atime)
    props["Read-only"] = "Yes" if not (st.st_mode & stat.S_IWRITE) else "No"
    if hasattr(st, "st_file_attributes"):
        attrs = st.st_file_attributes
        props["Hidden"] = "Yes" if attrs & stat.FILE_ATTRIBUTE_HIDDEN else "No"
        props["System"] = "Yes" if attrs & stat.FILE_ATTRIBUTE_SYSTEM else "No"
    try:
        md5, sha256 = _file_hashes(path)
    except OSError as exc:
        # A locked or permission-denied file still has useful metadata; report
        # the failure in place of the digests instead of losing the comparison.
        md5 = sha256 = f"(unreadable: {exc.__class__.__name__})"
    props["MD5"] = md5
    props["SHA-256"] = sha256
    return props


def embedded_metadata(path):
    """Return an OrderedDict of embedded metadata extracted by hachoir.

    Returns an empty dict when hachoir is unavailable, the format is not
    recognised, or parsing fails — the comparison degrades gracefully.
    """
    props = OrderedDict()
    if not HACHOIR_AVAILABLE:
        return props
    try:
        parser = createParser(str(path))
        if parser is None:
            return props
        with parser:
            meta = extractMetadata(parser)
        if meta is None:
            return props
        lines = meta.exportPlaintext(human=True)
    except Exception:
        return props

    group = None
    for line in lines or []:
        if line.startswith("- "):
            key, sep, value = line[2:].partition(": ")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if group:
                key = f"{group}{KEY_SEPARATOR}{key}"
            if key in props:
                if value not in props[key].split(" | "):
                    props[key] = props[key] + " | " + value
            else:
                props[key] = value
        else:
            header = line.rstrip(":").strip()
            group = None if header.lower() in ROOT_SECTION_HEADERS else header
    return props


def collect_metadata(path):
    """Return {section_name: OrderedDict(key -> value)} for one file."""
    sections = OrderedDict()
    sections["File system"] = filesystem_metadata(path)
    sections["Embedded metadata"] = embedded_metadata(path)
    return sections


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

def _ordered_union(first, second):
    seen = list(first)
    seen_set = set(seen)
    for item in second:
        if item not in seen_set:
            seen.append(item)
            seen_set.add(item)
    return seen


def compare_metadata(meta1, meta2):
    """Compare two metadata mappings produced by collect_metadata().

    Returns a list of (section, key, value1, value2, status) tuples.
    """
    rows = []
    for section in _ordered_union(meta1.keys(), meta2.keys()):
        props1 = meta1.get(section, {})
        props2 = meta2.get(section, {})
        for key in _ordered_union(props1.keys(), props2.keys()):
            in1, in2 = key in props1, key in props2
            v1 = props1.get(key, "")
            v2 = props2.get(key, "")
            if in1 and in2:
                status = STATUS_SAME if v1 == v2 else STATUS_DIFF
            elif in1:
                status = STATUS_ONLY_1
            else:
                status = STATUS_ONLY_2
            rows.append((section, key, v1, v2, status))
    return rows


def summarize(rows):
    counts = {STATUS_SAME: 0, STATUS_DIFF: 0, STATUS_ONLY_1: 0, STATUS_ONLY_2: 0}
    for row in rows:
        counts[row[4]] += 1
    return counts


def files_are_identical(rows):
    """True when both files hashed successfully to the same SHA-256."""
    for _sec, key, v1, _v2, status in rows:
        if key == "SHA-256":
            return status == STATUS_SAME and is_digest(v1)
    return False


def build_report(path1, path2, rows):
    """Plain-text comparison report suitable for the clipboard or a file."""
    counts = summarize(rows)
    lines = [
        f"{APP_NAME} report — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"File 1: {path1}",
        f"File 2: {path2}",
        (
            f"Summary: {counts[STATUS_SAME]} same, {counts[STATUS_DIFF]} different, "
            f"{counts[STATUS_ONLY_1] + counts[STATUS_ONLY_2]} present in only one file"
        ),
        "",
    ]
    section = None
    for sec, key, v1, v2, status in rows:
        if sec != section:
            section = sec
            lines.append(f"[{section}]")
        lines.append(f"  {key}")
        lines.append(f"    status: {status}")
        if status != STATUS_ONLY_2:
            lines.append(f"    file 1: {v1}")
        if status != STATUS_ONLY_1:
            lines.append(f"    file 2: {v2}")
    return "\n".join(lines)


def write_report(path, report):
    """Write *report* to *path* as UTF-8 text.

    Uses errors="replace" so a file name carrying unpaired surrogates — legal
    on NTFS — cannot turn saving into a silently truncated file.
    """
    with open(path, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(report)


# --------------------------------------------------------------------------
# Text fitting
# --------------------------------------------------------------------------

def fit_end(text, measure, max_px):
    """Shorten *text* with a trailing ellipsis until it fits *max_px*."""
    if measure(text) <= max_px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid] + "…") <= max_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…" if lo else "…"


def fit_middle(text, measure, max_px):
    """Shorten *text* with a middle ellipsis so both ends stay readable."""
    if measure(text) <= max_px:
        return text
    for keep in range(len(text) - 1, 0, -1):
        head = (keep + 1) // 2
        tail = keep - head
        candidate = text[:head] + "…" + (text[len(text) - tail:] if tail else "")
        if measure(candidate) <= max_px:
            return candidate
    return "…"


def _ellipsize(text, limit=MAX_CELL_LEN):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class DropZone(ttk.LabelFrame):
    """One file slot: drop target, browse/clear buttons, current selection."""

    def __init__(self, master, title, on_files):
        super().__init__(master, text=title, padding=8)
        self.on_files = on_files
        self.path = None
        self._fitted_width = 0

        # width=1 keeps the label from requesting space for its text, so the
        # text we fit to the current width can never push the window wider.
        self.label = tk.Label(
            self,
            relief="groove",
            borderwidth=2,
            width=1,
            height=3,
            anchor="center",
            justify="center",
            font="TkDefaultFont",
            cursor="hand2",
        )
        self.label.pack(fill="both", expand=True)
        self.label.bind("<Button-1>", lambda _e: self.browse())
        self.label.bind("<Configure>", self._on_label_configure)
        self._font = tkfont.nametofont("TkDefaultFont")

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(6, 0))
        browse = ttk.Button(buttons, text="Browse…", command=self.browse)
        browse.pack(side="left")
        clear = ttk.Button(buttons, text="Clear", command=self.clear)
        clear.pack(side="left", padx=(6, 0))

        self._render()

        if DND_AVAILABLE:
            # Register the whole slot, not just the inner label: a file released
            # over the frame's padding or the button strip should land in the
            # slot the user aimed at rather than falling through to the window.
            for widget in (self, self.label, buttons, browse, clear):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)

    # -- drop handling -----------------------------------------------------

    def _on_drop(self, event):
        paths = parse_drop_data(self, event.data)
        if paths:
            # Windows OLE drag-and-drop blocks the *source* application until
            # this callback returns, so hand off and return the action at once —
            # anything modal here would freeze the Explorer window.
            self.after(1, lambda: self.on_files(self, paths))
        return event.action

    # -- selection ---------------------------------------------------------

    def browse(self):
        path = filedialog.askopenfilename(title=f"Choose a file for {self.cget('text')}")
        if path:
            self.on_files(self, [path])

    def set_path(self, path):
        self.path = str(path)
        self._render()

    def clear(self):
        self.path = None
        self._render()
        self.on_files(self, [])

    # -- rendering ---------------------------------------------------------

    def _on_label_configure(self, event):
        if abs(event.width - self._fitted_width) < 8:
            return
        self._fitted_width = event.width
        self._render()

    def _render(self):
        available = max(self.label.winfo_width() - 12, 80)
        measure = self._font.measure
        if self.path is None:
            hint = "Drop a file here" if DND_AVAILABLE else "No file selected"
            lines = [hint, "or click to browse", ""]
        else:
            try:
                size = human_size(os.path.getsize(self.path))
            except OSError:
                size = "size unavailable"
            lines = [
                fit_end(os.path.basename(self.path), measure, available),
                size,
                fit_middle(self.path, measure, available),
            ]
        self.label.configure(text="\n".join(lines))


def parse_drop_data(widget, data):
    """Turn a tkdnd drop payload into a list of file paths."""
    try:
        items = widget.tk.splitlist(data)
    except tk.TclError:
        items = [data]
    return [item for item in items if item]


class MetaCompareApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self._apply_scaled_geometry()

        self._compare_seq = 0
        self._rows = []
        self._paths = (None, None)
        self._results = queue.Queue()

        self._build_ui()
        self._poll_results()

        if DND_AVAILABLE:
            # Catches drops that land outside either slot.
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_root_drop)

    def _apply_scaled_geometry(self):
        """Size the window in scaled pixels so it stays usable on high-DPI."""
        try:
            scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            scale = 1.0
        width = min(int(1000 * scale), self.root.winfo_screenwidth() - 40)
        height = min(int(680 * scale), self.root.winfo_screenheight() - 80)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(int(760 * scale), width), min(int(520 * scale), height))

    # -- layout ------------------------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        top.columnconfigure(0, weight=1, uniform="zones")
        top.columnconfigure(1, weight=1, uniform="zones")

        self.zone1 = DropZone(top, "File 1", self._zone_files)
        self.zone1.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.zone2 = DropZone(top, "File 2", self._zone_files)
        self.zone2.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        controls = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        controls.pack(fill="x")
        self.compare_btn = ttk.Button(controls, text="Compare", command=self.start_compare)
        self.compare_btn.pack(side="left")
        self.diff_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            controls,
            text="Show differences only",
            variable=self.diff_only,
            command=self._refresh_tree,
        ).pack(side="left", padx=(12, 0))
        self.copy_btn = ttk.Button(
            controls, text="Copy report", command=self.copy_report, state="disabled"
        )
        self.copy_btn.pack(side="left", padx=(12, 0))
        self.save_btn = ttk.Button(
            controls, text="Save report…", command=self.save_report, state="disabled"
        )
        self.save_btn.pack(side="left", padx=(6, 0))

        tree_frame = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        tree_frame.pack(fill="both", expand=True)

        columns = ("file1", "file2", "status")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings")
        self.tree.heading("#0", text="Property")
        self.tree.heading("file1", text="File 1")
        self.tree.heading("file2", text="File 2")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=230, minwidth=140)
        self.tree.column("file1", width=300, minwidth=120)
        self.tree.column("file2", width=300, minwidth=120)
        self.tree.column("status", width=90, minwidth=80, stretch=False)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self.tree.tag_configure("same", foreground="#1a7f37")
        self.tree.tag_configure("diff", foreground="#c62828")
        self.tree.tag_configure("only", foreground="#b26a00")
        self.tree.tag_configure("section", font=("TkDefaultFont", 9, "bold"))
        self.tree.bind("<Double-1>", self._show_row_detail)

        self.status_var = tk.StringVar()
        ttk.Label(
            self.root, textvariable=self.status_var, anchor="w", padding=(10, 4)
        ).pack(fill="x", side="bottom")
        self._set_idle_status()

    def _set_idle_status(self):
        self.status_var.set("Choose two files to compare their metadata.")

    # -- file selection ----------------------------------------------------

    def _zone_files(self, zone, paths):
        """A slot received file paths, or was cleared (empty *paths*)."""
        if not paths:
            self._invalidate_results()
            return
        self._deliver(zone, paths)

    def _on_root_drop(self, event):
        paths = parse_drop_data(self.root, event.data)
        if paths:
            zone = self._zone_at(event.x_root, event.y_root)
            self.root.after(1, lambda: self._deliver(zone, paths))
        return event.action

    def _zone_at(self, x_root, y_root):
        """The DropZone under the given screen coordinates, if any."""
        try:
            widget = self.root.winfo_containing(x_root, y_root)
        except (KeyError, tk.TclError):
            return None
        while widget is not None:
            if isinstance(widget, DropZone):
                return widget
            widget = getattr(widget, "master", None)
        return None

    def _deliver(self, zone, paths):
        """Assign dropped or browsed *paths* to slots and re-compare."""
        files = [p for p in paths if os.path.isfile(p)]
        rejected = [p for p in paths if not os.path.isfile(p)]

        if files:
            if len(files) > 1:
                # A pair of files always fills 1 then 2, whatever was aimed at.
                targets = [self.zone1, self.zone2]
            else:
                target = zone or (
                    self.zone1 if self.zone1.path is None else self.zone2
                )
                targets = [target]
            for target, path in zip(targets, files):
                target.set_path(path)
            self._invalidate_results()
            self._maybe_compare()

        if rejected:
            messagebox.showwarning(
                APP_NAME,
                "Only files can be compared — skipped:\n" + "\n".join(rejected[:5]),
            )

    def _maybe_compare(self):
        if self.zone1.path and self.zone2.path:
            self.start_compare()

    # -- comparison --------------------------------------------------------

    def _invalidate_results(self):
        """Drop results that no longer describe the selected files."""
        self._compare_seq += 1  # abandons any comparison still running
        self._rows = []
        self._paths = (None, None)
        self.tree.delete(*self.tree.get_children())
        self.copy_btn.state(["disabled"])
        self.save_btn.state(["disabled"])
        self.compare_btn.state(["!disabled"])
        self._set_idle_status()

    def start_compare(self):
        p1, p2 = self.zone1.path, self.zone2.path
        if not p1 or not p2:
            self._set_idle_status()
            return
        missing = [p for p in (p1, p2) if not os.path.isfile(p)]
        if missing:
            messagebox.showerror(
                APP_NAME, "This file is no longer available:\n" + "\n".join(missing)
            )
            return

        self._compare_seq += 1
        seq = self._compare_seq
        self.compare_btn.state(["disabled"])
        self.status_var.set("Comparing… (hashing large files can take a moment)")
        results = self._results

        def worker():
            try:
                rows = compare_metadata(collect_metadata(p1), collect_metadata(p2))
                results.put((seq, p1, p2, rows, None))
            except Exception as exc:
                results.put((seq, p1, p2, [], exc))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_results(self):
        """Hand worker results to the UI from the main thread.

        Tk must only be touched from the thread running the event loop, so the
        worker posts to a queue and this poller — not the worker — updates the
        widgets.
        """
        try:
            while True:
                self._compare_done(*self._results.get_nowait())
        except queue.Empty:
            pass
        self.root.after(POLL_INTERVAL_MS, self._poll_results)

    def _compare_done(self, seq, p1, p2, rows, error):
        if seq != self._compare_seq:
            return  # superseded by a newer comparison, or the slots changed
        self.compare_btn.state(["!disabled"])
        if error is not None:
            self.status_var.set("Comparison failed.")
            messagebox.showerror(
                APP_NAME,
                f"Could not compare these files:\n\n"
                f"{error.__class__.__name__}: {error}",
            )
            return
        self._rows = rows
        self._paths = (p1, p2)
        self.copy_btn.state(["!disabled"])
        self.save_btn.state(["!disabled"])
        self._refresh_tree()
        counts = summarize(rows)
        only = counts[STATUS_ONLY_1] + counts[STATUS_ONLY_2]
        identical = " Files are byte-identical." if files_are_identical(rows) else ""
        self.status_var.set(
            f"{counts[STATUS_SAME]} same · {counts[STATUS_DIFF]} different · "
            f"{only} present in only one file.{identical}"
        )

    # -- results view ------------------------------------------------------

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        diff_only = self.diff_only.get()
        section_node = None
        section = None
        shown = 0
        for index, (sec, key, v1, v2, status) in enumerate(self._rows):
            if diff_only and status == STATUS_SAME:
                continue
            if sec != section:
                section = sec
                section_node = self.tree.insert(
                    "", "end", iid=f"s{index}", text=sec, open=True, tags=("section",)
                )
            tag = {STATUS_SAME: "same", STATUS_DIFF: "diff"}.get(status, "only")
            # The row id carries its index, so the detail dialog can never show
            # a different row that happens to share this property name.
            self.tree.insert(
                section_node,
                "end",
                iid=f"r{index}",
                text=key,
                values=(_ellipsize(v1), _ellipsize(v2), status),
                tags=(tag,),
            )
            shown += 1
        if self._rows and shown == 0:
            self.tree.insert("", "end", text="No differences found", tags=("same",))

    def _show_row_detail(self, event):
        # Hit-test the click: focus() alone would reopen the last selected row
        # when the user double-clicks blank space or a column heading.
        if self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        item = self.tree.identify_row(event.y)
        if not item or not item.startswith("r"):
            return
        try:
            sec, key, v1, v2, status = self._rows[int(item[1:])]
        except (ValueError, IndexError):
            return
        messagebox.showinfo(
            f"{APP_NAME} — {key}",
            f"Section: {sec}\nStatus: {status}\n\n"
            f"File 1:\n{v1 or '(not present)'}\n\n"
            f"File 2:\n{v2 or '(not present)'}",
        )

    # -- report ------------------------------------------------------------

    def copy_report(self):
        if not self._rows:
            return
        report = build_report(self._paths[0], self._paths[1], self._rows)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(report)
        except tk.TclError as exc:
            messagebox.showerror(APP_NAME, f"Could not copy to the clipboard:\n{exc}")
            return
        self.status_var.set("Report copied to clipboard.")

    def save_report(self):
        if not self._rows:
            return
        path = filedialog.asksaveasfilename(
            title="Save comparison report",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="metacompare-report.txt",
        )
        if not path:
            return
        report = build_report(self._paths[0], self._paths[1], self._rows)
        try:
            write_report(path, report)
        except (OSError, UnicodeError) as exc:
            messagebox.showerror(APP_NAME, f"Could not save report:\n{exc}")
            return
        self.status_var.set(f"Report saved to {path}")


def _ensure_stdio():
    """Give sys.stdout/stderr somewhere to go.

    A --windowed PyInstaller build sets both to None, which turns any stray
    write — from a library or from Tk's exception reporter — into an
    AttributeError far from its cause.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass


def main():
    _ensure_stdio()
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
    MetaCompareApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
