# MetaCompare

A small, lightweight Windows app that compares the **metadata** of two files —
same or different formats — and gives you a color-coded readout of what
matches and what doesn't. Point it at two folders instead and it pairs up every
file whose name appears in both, so you can check a whole backup at a glance.

## Features

- **Drag & drop** two files into the window (or use *File…*)
- **Compare two folders at once**: drop a folder into each slot and every file
  whose name appears in both is compared as a pair. `IMG_1798.JPG` in one folder
  pairs with `IMG_1798.HEIC` in the other; names present in only one folder are
  ignored. Matching ignores case, and *Include subfolders* widens the search
- **Differences first**: each pair is one line with a verdict you can read at a
  glance, sorted so pairs that differ come first. Open one and the properties
  that actually disagree are already expanded; the file's full metadata sits
  under a collapsed *All metadata* node
- Differences that follow from being two different files — name, extension,
  size, hashes, container format — are kept in their own collapsed group, so a
  `.jpg` and a `.heic` of the same shot read as *no differences* rather than
  burying the one field that really changed
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
  file (orange), with a *Hide matching pairs* filter to show only what differs
- Double-click any row to see the full, untruncated values and both file paths
- Long scans report progress and can be stopped with *Cancel*; one unreadable
  file is reported against its own pair instead of ending the scan
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

The released exe is not code-signed, so Windows may show a SmartScreen warning
the first time you run it. If you would rather not take an unsigned binary on
trust, build your own from source below — it produces the same thing.

## From source

Run it:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python main.py
```

Build your own exe into `dist\MetaCompare.exe`:

```powershell
.\build.ps1
```

Run the tests:

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
4. Differing properties are then split by how much they tell you. A name, size,
   hash or container format differs whenever two files are not the same file, so
   those are separated from differences in the metadata the photo carries. Only
   the latter count toward a pair's verdict — otherwise every cross-format pair
   would report the same handful of differences and the view would say nothing.
5. Results render in a grouped tree with a summary line at the bottom.

Format-specific facts still show up as one-sided rows — a JPEG reports its JFIF
version and chroma subsampling, which a HEIC has no equivalent for. That is
real difference, not a gap in the comparison.

Hashing and parsing run on a background thread, so the UI stays responsive
even for large files.
