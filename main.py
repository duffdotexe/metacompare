"""MetaCompare — compare the metadata of two files side by side.

Drag and drop two files (or browse for them) and get a readout of which
metadata properties match and which differ. Covers file-system metadata
(size, timestamps, attributes, hashes) and embedded metadata (EXIF, ID3,
video/document properties) for any format hachoir can parse.
"""

import hashlib
import mimetypes
import os
import stat
import sys
import threading
import tkinter as tk
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

try:
    from hachoir.core import config as _hachoir_config
    from hachoir.metadata import extractMetadata
    from hachoir.parser import createParser
    _hachoir_config.quiet = True
    HACHOIR_AVAILABLE = True
except ImportError:
    HACHOIR_AVAILABLE = False

APP_NAME = "MetaCompare"
HASH_CHUNK = 1024 * 1024
MAX_CELL_LEN = 160

STATUS_SAME = "Same"
STATUS_DIFF = "Different"
STATUS_ONLY_1 = "Only file 1"
STATUS_ONLY_2 = "Only file 2"


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
    md5, sha256 = _file_hashes(path)
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
            body = line[2:]
            key, sep, value = body.partition(": ")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if group:
                key = f"{group} — {key}"
            if key in props:
                if value not in props[key].split(" | "):
                    props[key] = props[key] + " | " + value
            else:
                props[key] = value
        else:
            header = line.rstrip(":").strip()
            group = None if header.lower() == "metadata" else header
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


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class DropZone(ttk.LabelFrame):
    """One file slot: drop target, browse/clear buttons, current selection."""

    def __init__(self, master, title, on_files):
        super().__init__(master, text=title, padding=8)
        self.on_files = on_files
        self.path = None

        hint = "Drop a file here" if DND_AVAILABLE else "Use Browse to pick a file"
        self.label = tk.Label(
            self,
            text=hint,
            relief="groove",
            borderwidth=2,
            height=4,
            anchor="center",
            justify="center",
            wraplength=320,
            cursor="hand2",
        )
        self.label.pack(fill="both", expand=True)
        self.label.bind("<Button-1>", lambda _e: self.browse())

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Browse…", command=self.browse).pack(side="left")
        ttk.Button(buttons, text="Clear", command=self.clear).pack(side="left", padx=(6, 0))

        if DND_AVAILABLE:
            self.label.drop_target_register(DND_FILES)
            self.label.dnd_bind("<<Drop>>", self._on_drop)

    def _on_drop(self, event):
        paths = parse_drop_data(self, event.data)
        if paths:
            self.on_files(self, paths)
        return event.action

    def browse(self):
        path = filedialog.askopenfilename(title=f"Choose a file for {self.cget('text')}")
        if path:
            self.on_files(self, [path])

    def set_path(self, path):
        self.path = str(path)
        name = os.path.basename(self.path)
        try:
            size = human_size(os.path.getsize(self.path))
        except OSError:
            size = "?"
        self.label.configure(text=f"{name}\n{size}\n{self.path}")

    def clear(self):
        self.path = None
        hint = "Drop a file here" if DND_AVAILABLE else "Use Browse to pick a file"
        self.label.configure(text=hint)
        self.on_files(self, [])


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
        self.root.geometry("1000x680")
        self.root.minsize(760, 520)

        self._compare_seq = 0
        self._rows = []
        self._paths = (None, None)

        self._build_ui()
        if DND_AVAILABLE:
            # Dropping two files anywhere on the window fills both slots.
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_root_drop)

    # -- layout ------------------------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)

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

        self.status_var = tk.StringVar(
            value="Choose two files to compare their metadata."
        )
        status_bar = ttk.Label(
            self.root, textvariable=self.status_var, anchor="w", padding=(10, 4)
        )
        status_bar.pack(fill="x", side="bottom")

    # -- file selection ----------------------------------------------------

    def _zone_files(self, zone, paths):
        """A zone received file paths (possibly several) or was cleared."""
        if not paths:
            self._maybe_compare()
            return
        rejected = [p for p in paths if not os.path.isfile(p)]
        paths = [p for p in paths if os.path.isfile(p)]
        if rejected:
            messagebox.showwarning(
                APP_NAME,
                "Only files can be compared — skipped:\n"
                + "\n".join(rejected[:5]),
            )
        if not paths:
            return
        zone.set_path(paths[0])
        if len(paths) > 1:
            other = self.zone2 if zone is self.zone1 else self.zone1
            other.set_path(paths[1])
        self._maybe_compare()

    def _on_root_drop(self, event):
        paths = [p for p in parse_drop_data(self.root, event.data) if os.path.isfile(p)]
        if len(paths) >= 2:
            self.zone1.set_path(paths[0])
            self.zone2.set_path(paths[1])
            self._maybe_compare()
        elif len(paths) == 1:
            target = self.zone1 if self.zone1.path is None else self.zone2
            target.set_path(paths[0])
            self._maybe_compare()
        return event.action

    def _maybe_compare(self):
        if self.zone1.path and self.zone2.path:
            self.start_compare()

    # -- comparison --------------------------------------------------------

    def start_compare(self):
        p1, p2 = self.zone1.path, self.zone2.path
        if not p1 or not p2:
            self.status_var.set("Choose two files to compare their metadata.")
            return
        if not os.path.isfile(p1) or not os.path.isfile(p2):
            messagebox.showerror(APP_NAME, "One of the selected files no longer exists.")
            return

        self._compare_seq += 1
        seq = self._compare_seq
        self.compare_btn.state(["disabled"])
        self.status_var.set("Comparing… (hashing large files can take a moment)")

        def worker():
            try:
                rows = compare_metadata(collect_metadata(p1), collect_metadata(p2))
                self.root.after(0, lambda: self._compare_done(seq, p1, p2, rows, None))
            except Exception as exc:  # surface the failure in the UI
                self.root.after(0, lambda: self._compare_done(seq, p1, p2, [], exc))

        threading.Thread(target=worker, daemon=True).start()

    def _compare_done(self, seq, p1, p2, rows, error):
        if seq != self._compare_seq:
            return  # a newer comparison superseded this one
        self.compare_btn.state(["!disabled"])
        if error is not None:
            self.status_var.set("Comparison failed.")
            messagebox.showerror(APP_NAME, f"Could not compare files:\n{error}")
            return
        self._rows = rows
        self._paths = (p1, p2)
        self.copy_btn.state(["!disabled"])
        self.save_btn.state(["!disabled"])
        self._refresh_tree()
        counts = summarize(rows)
        only = counts[STATUS_ONLY_1] + counts[STATUS_ONLY_2]
        identical = " Files are byte-identical." if self._files_identical(rows) else ""
        self.status_var.set(
            f"{counts[STATUS_SAME]} same · {counts[STATUS_DIFF]} different · "
            f"{only} present in only one file.{identical}"
        )

    @staticmethod
    def _files_identical(rows):
        return any(
            key == "SHA-256" and status == STATUS_SAME
            for _sec, key, _v1, _v2, status in rows
        )

    # -- results view ------------------------------------------------------

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        diff_only = self.diff_only.get()
        section_node = None
        section = None
        shown = 0
        for sec, key, v1, v2, status in self._rows:
            if diff_only and status == STATUS_SAME:
                continue
            if sec != section:
                section = sec
                section_node = self.tree.insert(
                    "", "end", text=sec, open=True, tags=("section",)
                )
            tag = {
                STATUS_SAME: "same",
                STATUS_DIFF: "diff",
            }.get(status, "only")
            self.tree.insert(
                section_node,
                "end",
                text=key,
                values=(_ellipsize(v1), _ellipsize(v2), status),
                tags=(tag,),
            )
            shown += 1
        if self._rows and shown == 0:
            self.tree.insert("", "end", text="No differences found", tags=("same",))

    def _show_row_detail(self, _event):
        item = self.tree.focus()
        if not item or self.tree.parent(item) == "":
            return
        key = self.tree.item(item, "text")
        for sec, row_key, v1, v2, status in self._rows:
            if row_key == key:
                messagebox.showinfo(
                    f"{APP_NAME} — {key}",
                    f"Section: {sec}\nStatus: {status}\n\n"
                    f"File 1:\n{v1 or '(not present)'}\n\n"
                    f"File 2:\n{v2 or '(not present)'}",
                )
                return

    # -- report ------------------------------------------------------------

    def copy_report(self):
        if not self._rows:
            return
        report = build_report(self._paths[0], self._paths[1], self._rows)
        self.root.clipboard_clear()
        self.root.clipboard_append(report)
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
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(report)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not save report:\n{exc}")
            return
        self.status_var.set(f"Report saved to {path}")


def _ellipsize(text, limit=MAX_CELL_LEN):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def main():
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
