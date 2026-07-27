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
import re
import stat
import sys
import threading
import tkinter as tk
from collections import OrderedDict, namedtuple
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

try:
    from PIL import ExifTags, Image

    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

try:
    # hachoir has no HEIF parser, so a HEIC photo would otherwise report nothing
    # beyond its file-system properties.
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False

APP_NAME = "MetaCompare"
HASH_CHUNK = 1024 * 1024
MAX_CELL_LEN = 160
POLL_INTERVAL_MS = 80
KEY_SEPARATOR = " — "
# Above this, a folder scan is worth confirming before it reads every file.
LARGE_SCAN_PAIRS = 250

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


# hachoir's own spellings for the properties it also reports. Reusing them lets
# a photo Pillow can read line up against one only hachoir can parse — a camera
# raw file, say — instead of producing two one-sided rows.
HACHOIR_KEY_ALIASES = {
    "Make": "Camera manufacturer",
    "Model": "Camera model",
    "Software": "Producer",
    "FNumber": "Camera focal",  # hachoir files the f-number under this name
    "ExposureTime": "Camera exposure",
    "FocalLength": "Focal length",
    "ISOSpeedRatings": "ISO speed rating",
    "PhotographicSensitivity": "ISO speed rating",
    "DateTimeOriginal": "Date-time original",
    "DateTimeDigitized": "Date-time digitized",
    "Orientation": "Image orientation",
    "ExifVersion": "EXIF version",
    "FlashPixVersion": "Flashpix version",
    "ShutterSpeedValue": "Shutter speed",
    "ApertureValue": "Aperture",
    "BrightnessValue": "Camera brightness",
    "ExposureBiasValue": "Exposure bias",
    "FocalLengthIn35mmFilm": "Focal length in 35mm film",
}

# hachoir renders every EXIF rational with "%.3g". Matching that on the keys
# above keeps a shared property to one row instead of splitting it into two
# that differ only in precision.
THREE_SIGNIFICANT_FIGURE_TAGS = frozenset({
    "FNumber", "FocalLength", "ApertureValue", "MaxApertureValue",
    "ShutterSpeedValue", "BrightnessValue",
})

# Matches hachoir's table so the two agree on wording.
ORIENTATION_NAMES = {
    1: "Horizontal (normal)",
    2: "Mirrored horizontal",
    3: "Rotated 180",
    4: "Mirrored vertical",
    5: "Mirrored horizontal then rotated 90 counter-clock-wise",
    6: "Rotated 90 clock-wise",
    7: "Mirrored horizontal then rotated 90 clock-wise",
    8: "Rotated 90 counter clock-wise",
}

BITS_PER_MODE = {
    "1": 1, "L": 8, "LA": 16, "P": 8, "PA": 16, "RGB": 24, "RGBA": 32,
    "RGBX": 32, "CMYK": 32, "YCbCr": 24, "LAB": 24, "HSV": 24, "I": 32,
    "F": 32, "I;16": 16, "I;16B": 16, "I;16L": 16,
}

# Pointers, binary blobs, and duplicates of the real image dimensions.
SKIP_EXIF_TAGS = frozenset({
    "ExifOffset", "GPSInfo", "MakerNote", "PrintImageMatching",
    "ComponentsConfiguration", "InteroperabilityOffset",
    "ExifInteroperabilityOffset", "XMLPacket", "ImageResources", "IPTCNAA",
    "PhotoshopSettings", "ThumbnailOffset", "ThumbnailLength", "StripOffsets",
    "StripByteCounts", "ImageWidth", "ImageLength", "PixelXDimension",
    "PixelYDimension", "ExifImageWidth", "ExifImageHeight", "TileOffsets",
    "TileByteCounts", "JPEGInterchangeFormat", "JPEGInterchangeFormatLength",
    "OpcodeList1", "OpcodeList2", "OpcodeList3", "CFAPattern", "SceneType",
    "FileSource",
})

MAX_EXIF_TEXT = 200


def _deg_to_float(degree, minute, second):
    """hachoir's exact conversion, so decimal coordinates match bit for bit."""
    return degree + (float(minute) + float(second) / 60.0) / 60.0


def humanize_tag(name):
    """'LensModel' -> 'Lens model', 'ISOSpeedRatings' -> 'ISO speed ratings'."""
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z']*|[a-z']+|\d+", str(name))
    if not parts:
        return str(name)
    words = [parts[0]]
    for part in parts[1:]:
        words.append(part if part.isupper() and len(part) > 1 else part.lower())
    return " ".join(words)


def _exif_text(value, depth=0):
    """Render an EXIF value as readable text, or None to leave it out."""
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8").replace("\x00", "").strip()
        except UnicodeDecodeError:
            return None  # a binary blob is noise in a comparison
        return text or None
    if isinstance(value, (tuple, list)):
        if depth:
            return None  # nested sequences are structure, not information
        parts = [_exif_text(item, depth + 1) for item in value]
        if not parts or any(part is None for part in parts):
            return None
        return ", ".join(parts)
    if isinstance(value, str):
        text = value.replace("\x00", "").strip()
        return text or None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        text = str(value).replace("\x00", "").strip()
        return text or None
    if number.is_integer():
        return str(int(number))
    return f"{number:.6g}"


def _exif_datetime(value):
    """EXIF stamps are 'YYYY:MM:DD HH:MM:SS'; show them like every other date."""
    text = _exif_text(value)
    if not text:
        return None
    match = re.match(r"^(\d{4}):(\d{2}):(\d{2})[ T](.+)$", text)
    if match:
        y, m, d, rest = match.groups()
        return f"{y}-{m}-{d} {rest}"
    return text


def _format_exif_entry(tag, value):
    """Return (key, text) for one EXIF tag, or None to skip it."""
    if tag in SKIP_EXIF_TAGS:
        return None
    key = HACHOIR_KEY_ALIASES.get(tag, humanize_tag(tag))

    if tag == "Orientation":
        try:
            return key, ORIENTATION_NAMES.get(int(value), _exif_text(value))
        except (TypeError, ValueError):
            return None
    if tag == "ExposureTime":
        try:
            seconds = float(value)
            if seconds > 0:
                return key, "1/%g" % (1 / seconds)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        return None
    if tag in THREE_SIGNIFICANT_FIGURE_TAGS:
        try:
            return key, "%.3g" % float(value)
        except (TypeError, ValueError):
            return None
    if tag in ("ISOSpeedRatings", "PhotographicSensitivity"):
        if isinstance(value, (tuple, list)):
            value = value[0] if value else None
        text = _exif_text(value)
        return (key, text) if text else None
    if tag.startswith("DateTime") or tag in ("GPSDateStamp",):
        text = _exif_datetime(value)
        return (key, text) if text else None

    text = _exif_text(value)
    if text is None or len(text) > MAX_EXIF_TEXT:
        return None
    return key, text


def _gps_metadata(gps_ifd):
    """Decimal latitude/longitude/altitude plus the remaining GPS tags."""
    props = OrderedDict()
    named = {}
    for tag_id, value in gps_ifd.items():
        named[ExifTags.GPSTAGS.get(tag_id, str(tag_id))] = value

    for axis, ref_tag, value_tag, negative in (
        ("Latitude", "GPSLatitudeRef", "GPSLatitude", "S"),
        ("Longitude", "GPSLongitudeRef", "GPSLongitude", "W"),
    ):
        coords = named.get(value_tag)
        ref = _exif_text(named.get(ref_tag, "")) or ""
        if not coords or len(coords) != 3:
            continue
        try:
            degrees = _deg_to_float(*(float(part) for part in coords))
        except (TypeError, ValueError):
            continue
        if ref.upper().startswith(negative):
            degrees = -degrees
        props[axis] = repr(degrees)

    altitude = named.get("GPSAltitude")
    if altitude is not None:
        try:
            metres = float(altitude)
            if _exif_text(named.get("GPSAltitudeRef", 0)) == "1":
                metres = -metres
            props["Altitude"] = "%.1f meters" % metres
        except (TypeError, ValueError):
            pass

    handled = {
        "GPSLatitude", "GPSLatitudeRef", "GPSLongitude", "GPSLongitudeRef",
        "GPSAltitude", "GPSAltitudeRef", "GPSVersionID",
    }
    for tag, value in named.items():
        if tag in handled:
            continue
        if tag == "GPSTimeStamp" and isinstance(value, (tuple, list)):
            try:
                props["GPS time stamp"] = ":".join(
                    "%02d" % int(float(part)) for part in value
                )
                continue
            except (TypeError, ValueError):
                pass
        entry = _format_exif_entry(tag, value)
        if entry:
            props[entry[0]] = entry[1]
    return props


def image_metadata(path):
    """Normalized image and EXIF metadata, keyed identically across formats.

    This is what lets a .jpg and the .heic of the same photo line up: both are
    read through Pillow, so the two produce the same key names rather than each
    speaking its own format's vocabulary.
    """
    props = OrderedDict()
    if not PILLOW_AVAILABLE:
        return props
    # Own the handle: Image.open() raises before the `with` engages when it
    # cannot identify a file, which would leak the descriptor and keep a
    # Windows lock on every non-image file compared.
    handle = None
    try:
        handle = open(path, "rb")
        with Image.open(handle) as im:
            exif = im.getexif()

            # pillow-heif rotates a HEIC's pixels when it opens the file, resets
            # the EXIF orientation to 1, and records the real value here. Left
            # alone, that makes every portrait photo look different from its own
            # JPEG twin — different orientation and swapped dimensions — so
            # report the geometry and orientation as the file actually stores
            # them, which is what every other format reports.
            stored_orientation = im.info.get("original_orientation")
            try:
                stored_orientation = int(stored_orientation)
            except (TypeError, ValueError):
                stored_orientation = None

            width, height = im.size
            if stored_orientation in (5, 6, 7, 8):
                width, height = height, width
            props["Image width"] = "%s pixels" % width
            props["Image height"] = "%s pixels" % height

            orientation = stored_orientation
            if not orientation and exif:
                try:
                    orientation = int(exif.get(ExifTags.Base.Orientation))
                except (TypeError, ValueError):
                    orientation = None
            if orientation:
                props["Image orientation"] = ORIENTATION_NAMES.get(
                    orientation, str(orientation)
                )

            if im.format:
                props["Image format"] = im.format
                mime = Image.MIME.get(im.format)
                if mime:
                    props["MIME type"] = mime
            if im.mode:
                props["Color mode"] = im.mode
                if im.mode in BITS_PER_MODE:
                    props["Bits/pixel"] = str(BITS_PER_MODE[im.mode])
            frames = getattr(im, "n_frames", 1)
            if frames and frames > 1:
                props["Frame count"] = str(frames)
            if im.info.get("icc_profile"):
                props["ICC profile"] = "present (%d bytes)" % len(
                    im.info["icc_profile"]
                )

            if exif:
                for tag_id, value in exif.items():
                    entry = _format_exif_entry(
                        ExifTags.TAGS.get(tag_id, str(tag_id)), value
                    )
                    if entry:
                        props.setdefault(entry[0], entry[1])
                try:
                    exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
                except (AttributeError, KeyError, OSError, ValueError):
                    exif_ifd = None
                for tag_id, value in (exif_ifd or {}).items():
                    entry = _format_exif_entry(
                        ExifTags.TAGS.get(tag_id, str(tag_id)), value
                    )
                    if entry:
                        props.setdefault(entry[0], entry[1])
                try:
                    gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
                except (AttributeError, KeyError, OSError, ValueError):
                    gps_ifd = None
                if gps_ifd:
                    for key, value in _gps_metadata(gps_ifd).items():
                        props.setdefault(key, value)

            # hachoir reports a creation date for formats it parses; derive the
            # same key so those comparisons stay aligned.
            if "Date-time original" in props:
                props.setdefault("Creation date", props["Date-time original"])
            elif "Date time" in props:
                props.setdefault("Creation date", props["Date time"])

            if im.info.get("xmp"):
                props["XMP"] = "present (%d bytes)" % len(im.info["xmp"])
    except Exception:
        # Not an image, an unsupported variant, or a damaged file: the other
        # extractors still have something to say about it.
        return OrderedDict()
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
    return props


def collect_metadata(path):
    """Return {section_name: OrderedDict(key -> value)} for one file."""
    sections = OrderedDict()
    sections["File system"] = filesystem_metadata(path)

    # Normalized image metadata comes first and wins on conflicts: it is the
    # only extractor that names a JPEG's properties the same way it names a
    # HEIC's. hachoir then adds whatever it knows that Pillow does not, which
    # is everything for audio, video, documents and archives.
    merged = image_metadata(path)
    for key, value in embedded_metadata(path).items():
        merged.setdefault(key, value)
    sections["Embedded metadata"] = merged
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


class Comparison:
    """One pair of files and the result of comparing them."""

    __slots__ = ("path1", "path2", "rows", "error")

    def __init__(self, path1, path2, rows, error=None):
        self.path1 = path1
        self.path2 = path2
        self.rows = rows or []
        self.error = error

    @property
    def label(self):
        stem = os.path.splitext(os.path.basename(self.path1))[0]
        return stem or os.path.basename(self.path1)


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


# Properties that describe the file or its container rather than the photo
# inside it. Comparing a .jpg with the .heic of the same shot, every one of
# these differs by construction — different name, different bytes, different
# format — so counting them as differences buries the ones that mean something.
STRUCTURAL_KEYS = frozenset({
    # The file on disk, not what it depicts.
    "Name", "Extension", "MIME type (by extension)", "Encoding (by extension)",
    "Size", "MD5", "SHA-256", "Created", "Modified", "Accessed",
    "Read-only", "Hidden", "System",
    # The container format, not the image it carries.
    "Image format", "MIME type", "Compression", "Pixel format", "Endianness",
    "Format version", "Comment", "Tile width", "Tile length",
    # Reported by size only, and two formats serialize the same payload to
    # different byte counts — a difference here is a hint to look closer, not
    # evidence the photo's metadata disagrees.
    "XMP", "ICC profile",
})

RowGroups = namedtuple("RowGroups", "content structural one_sided same")


def partition_rows(rows):
    """Group rows by how much a difference in them actually tells you.

    ``content`` holds differing properties both files carry and that describe
    the photo itself — the ones worth looking at first. ``structural`` holds
    differences that follow from being two different files in two different
    formats. ``one_sided`` holds properties only one file records at all.
    """
    content, structural, one_sided, same = [], [], [], []
    for row in rows:
        status = row[4]
        if status == STATUS_SAME:
            same.append(row)
        elif status == STATUS_DIFF:
            if row[1] in STRUCTURAL_KEYS:
                structural.append(row)
            else:
                content.append(row)
        else:
            one_sided.append(row)
    return RowGroups(content, structural, one_sided, same)


def verdict(rows):
    """A few words summarizing one comparison, for an at-a-glance column."""
    if not rows:
        return "no metadata"
    groups = partition_rows(rows)
    if groups.content:
        return f"{len(groups.content)} different"
    if files_are_identical(rows):
        return "identical files"
    if groups.one_sided or groups.structural:
        return "no differences"
    return "all match"


# --------------------------------------------------------------------------
# Folder pairing
# --------------------------------------------------------------------------

def index_by_stem(folder, recursive=False):
    """Map each file name (without extension) to the files carrying it.

    Names are matched case-insensitively, so IMG_1798.JPG pairs with
    img_1798.heic the way Windows itself would treat them.
    """
    index = {}
    try:
        if recursive:
            walked = []
            for root, _dirs, files in os.walk(folder):
                walked.extend(os.path.join(root, name) for name in files)
        else:
            with os.scandir(folder) as entries:
                walked = [e.path for e in entries if e.is_file()]
    except OSError:
        return index
    for path in walked:
        stem, extension = os.path.splitext(os.path.basename(path))
        if not stem or not extension:
            continue  # no extension means nothing to pair across formats
        index.setdefault(stem.casefold(), []).append(path)
    for paths in index.values():
        paths.sort(key=str.casefold)
    return index


def find_matching_pairs(folder1, folder2, recursive=False):
    """Files whose name appears in both folders, as (path1, path2) pairs.

    A name present in only one folder is ignored, which is the point: two
    backup folders rarely hold the same set of shots.
    """
    index1 = index_by_stem(folder1, recursive)
    index2 = index_by_stem(folder2, recursive)
    pairs = []
    for stem in sorted(set(index1) & set(index2)):
        for path1 in index1[stem]:
            for path2 in index2[stem]:
                if _same_file(path1, path2):
                    continue  # comparing a file with itself proves nothing
                pairs.append((path1, path2))
    return pairs


def _same_file(path1, path2):
    try:
        return os.path.samefile(path1, path2)
    except OSError:
        return os.path.normcase(os.path.abspath(path1)) == os.path.normcase(
            os.path.abspath(path2)
        )


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------

def build_report(path1, path2, rows):
    """Plain-text comparison report suitable for the clipboard or a file."""
    return build_multi_report([Comparison(path1, path2, rows, None)])


def _comparison_report_lines(comparison):
    lines = [
        f"File 1: {comparison.path1}",
        f"File 2: {comparison.path2}",
    ]
    if comparison.error:
        lines.append(f"  could not be compared: {comparison.error}")
        return lines
    groups = partition_rows(comparison.rows)
    lines.append(
        f"Summary: {len(groups.content)} metadata differences, "
        f"{len(groups.structural)} file/format differences, "
        f"{len(groups.same)} same, "
        f"{len(groups.one_sided)} present in only one file"
    )
    if groups.content:
        lines.append("")
        lines.append("Metadata differences:")
        for _sec, key, v1, v2, _status in groups.content:
            lines.append(f"  {key}: {v1} | {v2}")
    lines.append("")
    section = None
    for sec, key, v1, v2, status in comparison.rows:
        if sec != section:
            section = sec
            lines.append(f"[{section}]")
        lines.append(f"  {key}")
        lines.append(f"    status: {status}")
        if status != STATUS_ONLY_2:
            lines.append(f"    file 1: {v1}")
        if status != STATUS_ONLY_1:
            lines.append(f"    file 2: {v2}")
    return lines


def build_multi_report(comparisons):
    """Report covering one comparison or a whole folder scan."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"{APP_NAME} report — {stamp}"]
    if len(comparisons) != 1:
        differing = sum(
            1 for c in comparisons if not c.error and partition_rows(c.rows).content
        )
        failed = sum(1 for c in comparisons if c.error)
        lines.append(
            f"{len(comparisons)} matching pairs · {differing} with differences"
            + (f" · {failed} could not be read" if failed else "")
        )
        lines.append("")
        lines.append("Overview:")
        for comparison in comparisons:
            name = os.path.basename(comparison.path1)
            other = os.path.basename(comparison.path2)
            state = comparison.error and "error" or verdict(comparison.rows)
            lines.append(f"  {name} ↔ {other}: {state}")
    lines.append("")
    for index, comparison in enumerate(comparisons):
        if len(comparisons) != 1:
            lines.append("=" * 70)
            lines.append(f"Pair {index + 1} of {len(comparisons)}")
        lines.extend(_comparison_report_lines(comparison))
        lines.append("")
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
    """One slot holding a file or a folder: drop target, buttons, selection."""

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
        browse = ttk.Button(buttons, text="File…", width=8, command=self.browse)
        browse.pack(side="left")
        browse_dir = ttk.Button(
            buttons, text="Folder…", width=9, command=self.browse_folder
        )
        browse_dir.pack(side="left", padx=(4, 0))
        clear = ttk.Button(buttons, text="Clear", width=7, command=self.clear)
        clear.pack(side="left", padx=(4, 0))

        self._render()

        if DND_AVAILABLE:
            # Register the whole slot, not just the inner label: a file released
            # over the frame's padding or the button strip should land in the
            # slot the user aimed at rather than falling through to the window.
            for widget in (self, self.label, buttons, browse, browse_dir, clear):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)

    # -- what is selected --------------------------------------------------

    @property
    def is_folder(self):
        return bool(self.path) and os.path.isdir(self.path)

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

    def browse_folder(self):
        path = filedialog.askdirectory(
            title=f"Choose a folder for {self.cget('text')}", mustexist=True
        )
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
            hint = "Drop a file or folder here" if DND_AVAILABLE else "Nothing selected"
            lines = [hint, "or click to browse", ""]
        elif self.is_folder:
            lines = [
                fit_end("📁 " + os.path.basename(self.path), measure, available),
                self._folder_summary(),
                fit_middle(self.path, measure, available),
            ]
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

    def _folder_summary(self):
        try:
            with os.scandir(self.path) as entries:
                files = sum(1 for entry in entries if entry.is_file())
        except OSError:
            return "folder"
        return f"folder — {files:,} file{'' if files == 1 else 's'}"


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
        self._comparisons = []
        self._results = queue.Queue()
        self._cancel = None
        self._row_ids = {}
        self._positions = {}

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

        self.zone1 = DropZone(top, "File or folder 1", self._zone_files)
        self.zone1.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.zone2 = DropZone(top, "File or folder 2", self._zone_files)
        self.zone2.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        controls = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        controls.pack(fill="x")
        self.compare_btn = ttk.Button(controls, text="Compare", command=self.start_compare)
        self.compare_btn.pack(side="left")
        self.cancel_btn = ttk.Button(
            controls, text="Cancel", command=self.cancel_compare, state="disabled"
        )
        self.cancel_btn.pack(side="left", padx=(6, 0))
        self.recursive = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            controls,
            text="Include subfolders",
            variable=self.recursive,
        ).pack(side="left", padx=(12, 0))
        self.diff_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            controls,
            text="Hide matching pairs",
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
        self.tree.tag_configure("pair", font=("TkDefaultFont", 9, "bold"),
                                foreground="#1a7f37")
        self.tree.tag_configure("pair_diff", font=("TkDefaultFont", 9, "bold"),
                                foreground="#c62828")
        self.tree.tag_configure("group", foreground="#555555")
        self.tree.tag_configure("failed", foreground="#c62828")
        self.tree.bind("<Double-1>", self._show_row_detail)

        self.status_var = tk.StringVar()
        ttk.Label(
            self.root, textvariable=self.status_var, anchor="w", padding=(10, 4)
        ).pack(fill="x", side="bottom")
        self._set_idle_status()

    def _set_idle_status(self):
        self.status_var.set(
            "Choose two files, or two folders to compare every file whose name "
            "appears in both."
        )

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
        usable = [p for p in paths if os.path.isfile(p) or os.path.isdir(p)]
        rejected = [p for p in paths if p not in usable]

        if usable:
            if len(usable) > 1:
                # A pair always fills slot 1 then 2, whatever was aimed at.
                targets = [self.zone1, self.zone2]
            else:
                target = zone or (
                    self.zone1 if self.zone1.path is None else self.zone2
                )
                targets = [target]
            for target, path in zip(targets, usable):
                target.set_path(path)
            self._invalidate_results()
            self._maybe_compare()

        if rejected:
            messagebox.showwarning(
                APP_NAME,
                "Only files and folders can be compared — skipped:\n"
                + "\n".join(rejected[:5]),
            )

    def _maybe_compare(self):
        if self.zone1.path and self.zone2.path:
            self.start_compare()

    # -- comparison --------------------------------------------------------

    def _invalidate_results(self):
        """Drop results that no longer describe the selected files."""
        self._compare_seq += 1  # abandons any comparison still running
        self.cancel_compare()
        self._comparisons = []
        self.tree.delete(*self.tree.get_children())
        self.copy_btn.state(["disabled"])
        self.save_btn.state(["disabled"])
        self.compare_btn.state(["!disabled"])
        self.cancel_btn.state(["disabled"])
        self._set_idle_status()

    def cancel_compare(self):
        if self._cancel is not None:
            self._cancel.set()

    def start_compare(self):
        p1, p2 = self.zone1.path, self.zone2.path
        if not p1 or not p2:
            self._set_idle_status()
            return

        folders = [os.path.isdir(p) for p in (p1, p2)]
        if any(folders) and not all(folders):
            messagebox.showerror(
                APP_NAME,
                "Choose either two files or two folders.\n\n"
                "With two folders, every file whose name appears in both is "
                "compared and the rest are ignored.",
            )
            return

        if all(folders):
            pairs = self._folder_pairs(p1, p2)
            if pairs is None:
                return
        else:
            missing = [p for p in (p1, p2) if not os.path.isfile(p)]
            if missing:
                messagebox.showerror(
                    APP_NAME,
                    "This file is no longer available:\n" + "\n".join(missing),
                )
                return
            pairs = [(p1, p2)]

        self._compare_seq += 1
        seq = self._compare_seq
        self._cancel = threading.Event()
        cancel = self._cancel
        self.compare_btn.state(["disabled"])
        self.cancel_btn.state(["!disabled"])
        total = len(pairs)
        self.status_var.set(
            f"Comparing {total} pair{'' if total == 1 else 's'}…"
            + (" (hashing large files can take a moment)" if total == 1 else "")
        )
        results = self._results

        def worker():
            comparisons = []
            for index, (first, second) in enumerate(pairs, 1):
                if cancel.is_set():
                    break
                results.put(("progress", seq, index, total, first))
                try:
                    rows = compare_metadata(
                        collect_metadata(first), collect_metadata(second)
                    )
                    comparisons.append(Comparison(first, second, rows))
                except Exception as exc:
                    # One unreadable file must not abandon the whole scan.
                    comparisons.append(
                        Comparison(
                            first, second, [], f"{exc.__class__.__name__}: {exc}"
                        )
                    )
            results.put(("done", seq, comparisons, cancel.is_set()))

        threading.Thread(target=worker, daemon=True).start()

    def _folder_pairs(self, folder1, folder2):
        """Matching pairs in two folders, or None if the scan should not run."""
        recursive = self.recursive.get()
        self.status_var.set("Scanning folders…")
        self.root.update_idletasks()
        try:
            pairs = find_matching_pairs(folder1, folder2, recursive)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not read the folders:\n{exc}")
            self._set_idle_status()
            return None
        if not pairs:
            self._set_idle_status()
            messagebox.showinfo(
                APP_NAME,
                "No file name appears in both folders, so there is nothing to "
                "compare."
                + ("" if recursive else "\n\nTry 'Include subfolders'."),
            )
            return None
        if len(pairs) > LARGE_SCAN_PAIRS and not messagebox.askokcancel(
            APP_NAME,
            f"Found {len(pairs):,} matching pairs.\n\n"
            "Comparing them all means reading every one of those files, which "
            "may take a while. You can stop partway with Cancel.\n\nContinue?",
        ):
            self._set_idle_status()
            return None
        return pairs

    def _poll_results(self):
        """Hand worker results to the UI from the main thread.

        Tk must only be touched from the thread running the event loop, so the
        worker posts to a queue and this poller — not the worker — updates the
        widgets.
        """
        try:
            while True:
                message = self._results.get_nowait()
                if message[0] == "progress":
                    self._compare_progress(*message[1:])
                else:
                    self._compare_done(*message[1:])
        except queue.Empty:
            pass
        self.root.after(POLL_INTERVAL_MS, self._poll_results)

    def _compare_progress(self, seq, index, total, path):
        if seq != self._compare_seq or total == 1:
            return
        self.status_var.set(
            f"Comparing {index:,} of {total:,} — {os.path.basename(path)}"
        )

    def _compare_done(self, seq, comparisons, cancelled):
        if seq != self._compare_seq:
            return  # superseded by a newer comparison, or the slots changed
        self.compare_btn.state(["!disabled"])
        self.cancel_btn.state(["disabled"])
        self._cancel = None
        self._comparisons = comparisons

        if not comparisons:
            self.status_var.set("Cancelled." if cancelled else "Nothing to compare.")
            self.tree.delete(*self.tree.get_children())
            return

        self.copy_btn.state(["!disabled"])
        self.save_btn.state(["!disabled"])
        self._refresh_tree()
        self.status_var.set(self._summary_text(comparisons, cancelled))

    @staticmethod
    def _summary_text(comparisons, cancelled=False):
        prefix = "Cancelled after " if cancelled else ""
        if len(comparisons) == 1:
            single = comparisons[0]
            if single.error:
                return f"Could not compare these files: {single.error}"
            groups = partition_rows(single.rows)
            identical = (
                " Files are byte-identical."
                if files_are_identical(single.rows)
                else ""
            )
            count = len(groups.content)
            return (
                f"{count} metadata difference{'' if count == 1 else 's'} · "
                f"{len(groups.structural)} file/format · "
                f"{len(groups.same)} same · "
                f"{len(groups.one_sided)} in only one file.{identical}"
            )
        differing = sum(
            1 for c in comparisons if not c.error and partition_rows(c.rows).content
        )
        failed = sum(1 for c in comparisons if c.error)
        text = (
            f"{prefix}{len(comparisons):,} pairs · {differing:,} with differences "
            f"· {len(comparisons) - differing - failed:,} matching"
        )
        return text + (f" · {failed:,} unreadable." if failed else ".")

    # -- results view ------------------------------------------------------

    def _refresh_tree(self):
        """Render results differences-first, with the full data tucked away.

        Pairs that differ sort to the top, and within a pair the properties both
        files carry come before the ones only one of them records. Everything
        else lives under a collapsed 'All metadata' node.
        """
        self.tree.delete(*self.tree.get_children())
        self._row_ids = {}
        comparisons = self._comparisons
        if not comparisons:
            return

        def rank(item):
            _index, comparison = item
            groups = partition_rows(comparison.rows)
            return (
                0 if comparison.error else 1,
                -len(groups.content),
                -len(groups.one_sided),
                comparison.label.casefold(),
            )

        ordered = sorted(enumerate(comparisons), key=rank)
        hide_matching = self.diff_only.get()
        shown = 0

        for index, comparison in ordered:
            groups = partition_rows(comparison.rows)
            if hide_matching and not comparison.error and not groups.content:
                continue
            shown += 1
            # Every comparison gets a header row, even a lone one: its verdict
            # is the whole answer at a glance.
            parent = self._insert_pair_node(index, comparison)
            if comparison.error:
                self.tree.insert(
                    parent,
                    "end",
                    text="Could not be compared",
                    values=(_ellipsize(comparison.error), "", "Error"),
                    tags=("failed",),
                )
                continue
            self._insert_groups(parent, index, comparison, groups)

        if not shown:
            self.tree.insert(
                "", "end", text="Every matching pair agrees", tags=("same",)
            )

    def _insert_pair_node(self, index, comparison):
        """One collapsed line per pair: the names and a one-glance verdict."""
        state = "Error" if comparison.error else verdict(comparison.rows)
        differs = bool(
            comparison.error or partition_rows(comparison.rows).content
        )
        return self.tree.insert(
            "",
            "end",
            iid=f"p{index}",
            text=comparison.label,
            open=False,
            values=(
                os.path.basename(comparison.path1),
                os.path.basename(comparison.path2),
                state,
            ),
            tags=("pair_diff" if differs else "pair",),
        )

    def _insert_groups(self, parent, index, comparison, groups):
        # Row identity by object, so a row that happens to equal another still
        # resolves to its own position in the comparison.
        self._positions = {
            id(row): position for position, row in enumerate(comparison.rows)
        }
        if groups.content:
            node = self.tree.insert(
                parent,
                "end",
                text="Metadata differences",
                open=False,
                values=("", "", f"{len(groups.content)}"),
                tags=("group",),
            )
            self._insert_rows(node, index, comparison, groups.content)
        else:
            self.tree.insert(
                parent,
                "end",
                text="Metadata matches",
                values=("", "", ""),
                tags=("same",),
            )

        if groups.structural:
            node = self.tree.insert(
                parent,
                "end",
                text="File and format differences",
                open=False,  # expected of two files in two formats
                values=("", "", f"{len(groups.structural)}"),
                tags=("group",),
            )
            self._insert_rows(node, index, comparison, groups.structural)

        if groups.one_sided:
            node = self.tree.insert(
                parent,
                "end",
                text="In only one file",
                open=False,
                values=("", "", f"{len(groups.one_sided)}"),
                tags=("group",),
            )
            self._insert_rows(node, index, comparison, groups.one_sided)

        if comparison.rows:
            node = self.tree.insert(
                parent,
                "end",
                text="All metadata",
                open=False,
                values=("", "", f"{len(comparison.rows)}"),
                tags=("group",),
            )
            section = None
            section_node = node
            for row in comparison.rows:
                if row[0] != section:
                    section = row[0]
                    section_node = self.tree.insert(
                        node, "end", text=section, open=False, tags=("section",)
                    )
                self._insert_rows(section_node, index, comparison, [row])

    def _insert_rows(self, parent, index, comparison, rows):
        for row in rows:
            sec, key, v1, v2, status = row
            tag = {STATUS_SAME: "same", STATUS_DIFF: "diff"}.get(status, "only")
            # Every row is registered against its tree id, so the detail dialog
            # resolves the exact row clicked. A row appears twice — once in its
            # group and once under 'All metadata' — so ids cannot encode
            # position alone.
            iid = f"r{len(self._row_ids)}"
            self._row_ids[iid] = (index, self._positions[id(row)])
            self.tree.insert(
                parent,
                "end",
                iid=iid,
                text=key,
                values=(_ellipsize(v1), _ellipsize(v2), status),
                tags=(tag,),
            )

    def _show_row_detail(self, event):
        # Hit-test the click: focus() alone would reopen the last selected row
        # when the user double-clicks blank space or a column heading.
        if self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
            return
        item = self.tree.identify_row(event.y)
        located = self._row_ids.get(item)
        if located is None:
            return
        try:
            comparison = self._comparisons[located[0]]
            sec, key, v1, v2, status = comparison.rows[located[1]]
        except IndexError:
            return
        messagebox.showinfo(
            f"{APP_NAME} — {key}",
            f"Section: {sec}\nStatus: {status}\n\n"
            f"File 1: {comparison.path1}\n{v1 or '(not present)'}\n\n"
            f"File 2: {comparison.path2}\n{v2 or '(not present)'}",
        )

    # -- report ------------------------------------------------------------

    def copy_report(self):
        if not self._comparisons:
            return
        report = build_multi_report(self._comparisons)
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(report)
        except tk.TclError as exc:
            messagebox.showerror(APP_NAME, f"Could not copy to the clipboard:\n{exc}")
            return
        self.status_var.set("Report copied to clipboard.")

    def save_report(self):
        if not self._comparisons:
            return
        path = filedialog.asksaveasfilename(
            title="Save comparison report",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="metacompare-report.txt",
        )
        if not path:
            return
        report = build_multi_report(self._comparisons)
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
