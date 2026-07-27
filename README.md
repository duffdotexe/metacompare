# MetaCompare

A small, lightweight Windows app that compares the **metadata** of two files —
same or different formats — and gives you a color-coded readout of what
matches and what doesn't.

## Features

- **Drag & drop** two files into the window (or use *Browse…*)
- Compares **file-system metadata**: name, extension, size, created/modified/
  accessed timestamps, read-only/hidden/system attributes, MIME type, and
  MD5 / SHA-256 content hashes
- Compares **embedded metadata**: EXIF/GPS for photos (via
  [Pillow](https://python-pillow.org) and
  [pillow-heif](https://github.com/bigcat88/pillow_heif)), plus ID3 for audio,
  duration/codec for video, document properties, and archive info (via
  [hachoir](https://github.com/vstinner/hachoir)) — across dozens of formats
- **Photo-friendly across formats**: a `.jpg` and the `.heic` of the same shot
  are read through one code path, so camera, lens, timestamps, exposure, ISO
  and GPS line up as matching rows instead of two columns of unrelated names.
  Portrait photos are handled correctly — HEIC decoders rotate the image and
  rewrite the orientation tag, so MetaCompare reports the geometry as stored
- Color-coded results: same (green) / different (red) / present in only one
  file (orange), with a *"Show differences only"* filter
- Double-click any row to see the full, untruncated values
- **Copy** the comparison report to the clipboard or **save** it as a text file
- Tells you outright when two files are **byte-identical** (matching SHA-256)
- Keeps working on awkward inputs: a locked or unreadable file still reports
  every property it can, with the hashes marked unreadable rather than failing
  the whole comparison

**Note** this project was created as a way to help save me some time when sorting through backup archives.
I made the project public because I figured it could be useful to some other people.
This is an entirely vibe coded project, and I don't plan on maintaining it unless there is demand.
Please use at your own risk.
-Duff

## Download

Grab `MetaCompare.exe` from the
[latest release](https://github.com/duffdotexe/metacompare/releases) —
no installation required. (Builds are produced by the GitHub Actions workflow
in this repo; every tagged `v*` push publishes a release automatically.)

## Run from source

```powershell
py -3 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python main.py
```

## Build the executable yourself

```powershell
.\build.ps1
```

The exe lands in `dist\MetaCompare.exe`.

## Tests

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -t .
```

## How it works

`main.py` is the whole app (Tkinter GUI + comparison logic):

1. Each file's metadata is collected into sections — *File system* (from
   `os.stat` + hashing) and *Embedded metadata*.
2. Embedded metadata comes from two extractors. Images go through Pillow, which
   normalizes EXIF into the same key names whatever the container is; hachoir
   then adds anything Pillow does not report, which is everything for audio,
   video, documents and archives. Where the two describe the same property, the
   normalized extractor reuses hachoir's own wording and value formatting, so a
   photo Pillow reads still lines up against one only hachoir can parse.
3. The two collections are compared key-by-key; every property gets a status:
   `Same`, `Different`, `Only file 1`, or `Only file 2`.
4. Results render in a grouped tree with a summary line at the bottom.

Format-specific facts still show up as one-sided rows — a JPEG reports its JFIF
version and chroma subsampling, which a HEIC has no equivalent for. That is
real difference, not a gap in the comparison.

Hashing and parsing run on a background thread, so the UI stays responsive
even for large files.
