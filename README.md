# MetaCompare

A small, lightweight Windows app that compares the **metadata** of two files —
same or different formats — and gives you a color-coded readout of what
matches and what doesn't.

## Features

- **Drag & drop** two files into the window (or use *Browse…*)
- Compares **file-system metadata**: name, extension, size, created/modified/
  accessed timestamps, read-only/hidden/system attributes, MIME type, and
  MD5 / SHA-256 content hashes
- Compares **embedded metadata** (via [hachoir](https://github.com/vstinner/hachoir)):
  EXIF for images, ID3 for audio, duration/codec for video, document
  properties, archive info, and more — across dozens of formats
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
   `os.stat` + hashing) and *Embedded metadata* (parsed by hachoir).
2. The two collections are compared key-by-key; every property gets a status:
   `Same`, `Different`, `Only file 1`, or `Only file 2`.
3. Results render in a grouped tree with a summary line at the bottom.

Hashing and parsing run on a background thread, so the UI stays responsive
even for large files.
