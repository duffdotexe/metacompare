import os
import sys
import tempfile
import unittest
import wave
from collections import OrderedDict

import main
from main import (
    STATUS_DIFF,
    STATUS_ONLY_1,
    STATUS_ONLY_2,
    STATUS_SAME,
    build_report,
    collect_metadata,
    compare_metadata,
    files_are_identical,
    fit_end,
    fit_middle,
    human_size,
    summarize,
    write_report,
)

# 1x1 transparent PNG.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d4944415478da63fcffff3f030005fe02fea75481840000000049454e44ae426082"
)


class HumanSizeTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(512), "512 B")

    def test_kilobytes(self):
        self.assertEqual(human_size(2048), "2.00 KB")

    def test_large(self):
        self.assertTrue(human_size(5 * 1024**4).endswith("TB"))


class CompareTests(unittest.TestCase):
    def _meta(self, props):
        return OrderedDict([("File system", OrderedDict(props))])

    def test_same_and_different(self):
        m1 = self._meta([("Size", "10"), ("MD5", "abc")])
        m2 = self._meta([("Size", "10"), ("MD5", "def")])
        rows = compare_metadata(m1, m2)
        by_key = {key: status for _s, key, _v1, _v2, status in rows}
        self.assertEqual(by_key["Size"], STATUS_SAME)
        self.assertEqual(by_key["MD5"], STATUS_DIFF)

    def test_only_in_one(self):
        m1 = self._meta([("A", "1")])
        m2 = self._meta([("B", "2")])
        rows = compare_metadata(m1, m2)
        by_key = {key: status for _s, key, _v1, _v2, status in rows}
        self.assertEqual(by_key["A"], STATUS_ONLY_1)
        self.assertEqual(by_key["B"], STATUS_ONLY_2)

    def test_key_order_is_union_preserving(self):
        m1 = self._meta([("A", "1"), ("B", "2")])
        m2 = self._meta([("B", "2"), ("C", "3")])
        rows = compare_metadata(m1, m2)
        self.assertEqual([r[1] for r in rows], ["A", "B", "C"])

    def test_summarize(self):
        rows = [
            ("s", "a", "1", "1", STATUS_SAME),
            ("s", "b", "1", "2", STATUS_DIFF),
            ("s", "c", "1", "", STATUS_ONLY_1),
        ]
        counts = summarize(rows)
        self.assertEqual(counts[STATUS_SAME], 1)
        self.assertEqual(counts[STATUS_DIFF], 1)
        self.assertEqual(counts[STATUS_ONLY_1], 1)
        self.assertEqual(counts[STATUS_ONLY_2], 0)


class TempFileTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def write(self, name, data):
        target = self.path(name)
        with open(target, "wb") as fh:
            fh.write(data)
        return target

    def write_wav(self, name="sound.wav", frames=4410):
        target = self.path(name)
        with wave.open(target, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x00\x00\x00" * frames)
        return target


class EndToEndTests(TempFileTestCase):
    def test_identical_content_same_hashes(self):
        p1 = self.write("a.txt", b"hello world")
        p2 = self.write("b.txt", b"hello world")
        rows = compare_metadata(collect_metadata(p1), collect_metadata(p2))
        by_key = {key: status for _s, key, _v1, _v2, status in rows}
        self.assertEqual(by_key["SHA-256"], STATUS_SAME)
        self.assertEqual(by_key["MD5"], STATUS_SAME)
        self.assertEqual(by_key["Size"], STATUS_SAME)
        self.assertEqual(by_key["Name"], STATUS_DIFF)
        self.assertTrue(files_are_identical(rows))

    def test_different_content_different_hashes(self):
        p1 = self.write("a.txt", b"hello")
        p2 = self.write("b.txt", b"goodbye")
        rows = compare_metadata(collect_metadata(p1), collect_metadata(p2))
        by_key = {key: status for _s, key, _v1, _v2, status in rows}
        self.assertEqual(by_key["SHA-256"], STATUS_DIFF)
        self.assertEqual(by_key["Size"], STATUS_DIFF)
        self.assertFalse(files_are_identical(rows))

    def test_report_contains_paths_and_keys(self):
        p1 = self.write("a.txt", b"x")
        p2 = self.write("b.txt", b"y")
        rows = compare_metadata(collect_metadata(p1), collect_metadata(p2))
        report = build_report(p1, p2, rows)
        self.assertIn(p1, report)
        self.assertIn(p2, report)
        self.assertIn("SHA-256", report)
        self.assertIn("Summary:", report)

    def test_embedded_metadata_on_png(self):
        p1 = self.write("one.png", PNG_BYTES)
        meta = main.embedded_metadata(p1)
        if main.HACHOIR_AVAILABLE:
            self.assertTrue(
                any("width" in k.lower() for k in meta),
                f"expected image width in embedded metadata, got: {list(meta)}",
            )


class EmbeddedSectionTests(TempFileTestCase):
    """hachoir heads a file's ungrouped metadata 'Metadata' or 'Common'."""

    def setUp(self):
        super().setUp()
        if not main.HACHOIR_AVAILABLE:
            self.skipTest("hachoir not installed")

    def test_root_metadata_keys_are_not_group_prefixed(self):
        # A WAV's root section is titled "Common:", a PNG's "Metadata:".
        for path in (self.write_wav(), self.write("one.png", PNG_BYTES)):
            meta = main.embedded_metadata(path)
            self.assertTrue(meta, f"no embedded metadata for {path}")
            for key in meta:
                self.assertNotIn(
                    main.KEY_SEPARATOR,
                    key,
                    f"root key {key!r} was wrongly prefixed with its section title",
                )

    def test_cross_format_keys_align(self):
        """A PNG and a WAV must compare their shared keys, not talk past each other."""
        png = self.write("one.png", PNG_BYTES)
        wav = self.write_wav()
        rows = compare_metadata(collect_metadata(png), collect_metadata(wav))
        embedded = {
            key: status
            for sec, key, _v1, _v2, status in rows
            if sec == "Embedded metadata"
        }
        self.assertEqual(embedded.get("MIME type"), STATUS_DIFF)
        self.assertEqual(embedded.get("Endianness"), STATUS_DIFF)

    def test_hachoir_never_writes_to_stdio(self):
        """A --windowed exe has no stdout/stderr for hachoir to write to."""
        self.assertFalse(main._hachoir_log.use_print)


class UnreadableFileTests(TempFileTestCase):
    def test_hash_failure_yields_placeholder_not_an_error(self):
        original = main._file_hashes

        def boom(_path):
            raise PermissionError(13, "Permission denied")

        main._file_hashes = boom
        try:
            props = main.filesystem_metadata(self.write("a.txt", b"data"))
        finally:
            main._file_hashes = original
        self.assertIn("unreadable", props["SHA-256"])
        self.assertIn("Size", props)  # the rest of the metadata still arrives

    def test_two_unreadable_files_are_not_called_identical(self):
        original = main._file_hashes

        def boom(_path):
            raise PermissionError(13, "Permission denied")

        main._file_hashes = boom
        try:
            p1 = self.write("a.txt", b"one")
            p2 = self.write("b.txt", b"two-different")
            rows = compare_metadata(collect_metadata(p1), collect_metadata(p2))
        finally:
            main._file_hashes = original
        by_key = {key: status for _s, key, _v1, _v2, status in rows}
        # Both placeholders match, but that is not evidence the files match.
        self.assertEqual(by_key["SHA-256"], STATUS_SAME)
        self.assertFalse(files_are_identical(rows))

    @unittest.skipUnless(sys.platform == "win32", "file locking is Windows-specific")
    def test_locked_file_still_compares(self):
        import msvcrt

        p1 = self.write("locked.bin", b"x" * 4096)
        p2 = self.write("plain.bin", b"y" * 4096)
        with open(p1, "r+b") as holder:
            msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 4096)
            try:
                rows = compare_metadata(collect_metadata(p1), collect_metadata(p2))
            finally:
                msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 4096)
        by_key = {key: v1 for _s, key, v1, _v2, _st in rows}
        self.assertIn("unreadable", by_key["SHA-256"])
        self.assertFalse(files_are_identical(rows))


class WriteReportTests(TempFileTestCase):
    def test_plain_report_round_trips(self):
        target = self.path("report.txt")
        write_report(target, "line one\nline two")
        with open(target, encoding="utf-8") as fh:
            self.assertIn("line one", fh.read())

    def test_surrogate_in_report_does_not_lose_the_file(self):
        """NTFS allows names with unpaired surrogates; saving must still work."""
        target = self.path("report.txt")
        write_report(target, "File 1: bad\udc80name.txt\nstatus: Same")
        self.assertGreater(os.path.getsize(target), 0)
        with open(target, encoding="utf-8") as fh:
            self.assertIn("status: Same", fh.read())


class HumanizeTagTests(unittest.TestCase):
    def test_camel_case_becomes_a_sentence(self):
        self.assertEqual(main.humanize_tag("LensModel"), "Lens model")
        self.assertEqual(main.humanize_tag("WhiteBalance"), "White balance")

    def test_acronyms_stay_upper_case(self):
        self.assertEqual(main.humanize_tag("ISOSpeedRatings"), "ISO speed ratings")
        self.assertEqual(main.humanize_tag("GPSImgDirection"), "GPS img direction")

    def test_single_word_unchanged(self):
        self.assertEqual(main.humanize_tag("Flash"), "Flash")


class ImageMetadataTests(TempFileTestCase):
    """A .jpg and the .heic of the same photo must line up key for key."""

    IDENTITY_KEYS = (
        "Camera manufacturer", "Camera model", "Producer", "Date-time original",
        "Creation date", "Camera exposure", "Camera focal", "Focal length",
        "ISO speed rating", "Lens model", "Latitude", "Longitude", "Altitude",
        "Image width", "Image height", "Image orientation", "Color mode",
    )

    def setUp(self):
        super().setUp()
        if not main.PILLOW_AVAILABLE:
            self.skipTest("Pillow not installed")
        from PIL import ExifTags, Image

        self.Image = Image
        self.ExifTags = ExifTags

    def _exif_blob(self, orientation=1):
        Image, ExifTags = self.Image, self.ExifTags
        exif = Image.Exif()
        exif[ExifTags.Base.Make] = "Apple"
        exif[ExifTags.Base.Model] = "iPhone 15 Pro"
        exif[ExifTags.Base.Software] = "18.1"
        exif[ExifTags.Base.DateTime] = "2026:03:14 09:26:53"
        exif[ExifTags.Base.Orientation] = orientation
        sub = exif.get_ifd(ExifTags.IFD.Exif)
        sub[ExifTags.Base.DateTimeOriginal] = "2026:03:14 09:26:53"
        sub[ExifTags.Base.ExposureTime] = 1 / 120
        sub[ExifTags.Base.FNumber] = 1.78
        sub[ExifTags.Base.ISOSpeedRatings] = 64
        sub[ExifTags.Base.FocalLength] = 6.86
        sub[ExifTags.Base.LensModel] = "iPhone 15 Pro back camera 6.86mm f/1.78"
        # Properties hachoir also reports, under its own spellings.
        sub[ExifTags.Base.DateTimeDigitized] = "2026:03:14 09:26:53"
        sub[ExifTags.Base.ShutterSpeedValue] = 4.90712
        sub[ExifTags.Base.ApertureValue] = 1.7
        sub[ExifTags.Base.BrightnessValue] = -0.43684
        sub[ExifTags.Base.ExposureBiasValue] = 0
        sub[ExifTags.Base.FocalLengthIn35mmFilm] = 49
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        gps[ExifTags.GPS.GPSLatitudeRef] = "N"
        gps[ExifTags.GPS.GPSLatitude] = (40.0, 44.0, 54.30)
        gps[ExifTags.GPS.GPSLongitudeRef] = "W"
        gps[ExifTags.GPS.GPSLongitude] = (73.0, 59.0, 8.90)
        gps[ExifTags.GPS.GPSAltitude] = 101.3
        return exif.tobytes()

    def _pair(self, orientation=1):
        """The same image and EXIF written as both a JPEG and a HEIC."""
        img = self.Image.new("RGB", (64, 48), (120, 160, 200))
        blob = self._exif_blob(orientation)
        jpg = self.path(f"photo{orientation}.jpg")
        heic = self.path(f"photo{orientation}.heic")
        img.save(jpg, "JPEG", quality=90, exif=blob)
        img.save(heic, "HEIF", quality=90, exif=blob)
        return jpg, heic

    def test_jpeg_exif_is_extracted(self):
        jpg, _ = self._pair()
        meta = main.image_metadata(jpg)
        self.assertEqual(meta["Camera model"], "iPhone 15 Pro")
        self.assertEqual(meta["Camera exposure"], "1/120")
        self.assertEqual(meta["Camera focal"], "1.78")
        self.assertEqual(meta["ISO speed rating"], "64")
        self.assertEqual(meta["Date-time original"], "2026-03-14 09:26:53")

    def test_heic_metadata_is_not_empty(self):
        """The bug this fixes: a HEIC reported nothing but file-system data."""
        if not main.HEIF_AVAILABLE:
            self.skipTest("pillow-heif not installed")
        _, heic = self._pair()
        meta = main.image_metadata(heic)
        self.assertTrue(meta, "no metadata extracted from the HEIC at all")
        self.assertEqual(meta["Camera model"], "iPhone 15 Pro")
        self.assertEqual(meta["Date-time original"], "2026-03-14 09:26:53")

    def test_heic_and_jpeg_identity_fields_match(self):
        if not main.HEIF_AVAILABLE:
            self.skipTest("pillow-heif not installed")
        jpg, heic = self._pair()
        rows = compare_metadata(collect_metadata(jpg), collect_metadata(heic))
        found = {
            key: (v1, v2, status)
            for sec, key, v1, v2, status in rows
            if sec == "Embedded metadata"
        }
        for key in self.IDENTITY_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, found, f"{key} missing from the comparison")
                v1, v2, status = found[key]
                self.assertEqual(
                    status, STATUS_SAME, f"{key}: {v1!r} vs {v2!r}"
                )

    def test_portrait_photos_are_not_falsely_different(self):
        """pillow-heif rotates HEIC pixels and resets the EXIF orientation."""
        if not main.HEIF_AVAILABLE:
            self.skipTest("pillow-heif not installed")
        for orientation in (6, 8):
            with self.subTest(orientation=orientation):
                jpg, heic = self._pair(orientation)
                rows = compare_metadata(
                    collect_metadata(jpg), collect_metadata(heic)
                )
                geometry = {
                    key: (v1, v2, status)
                    for _s, key, v1, v2, status in rows
                    if key in ("Image width", "Image height", "Image orientation")
                }
                for key, (v1, v2, status) in geometry.items():
                    self.assertEqual(
                        status, STATUS_SAME, f"{key}: {v1!r} vs {v2!r}"
                    )
                self.assertEqual(geometry["Image width"][0], "64 pixels")

    def test_gps_matches_hachoirs_arithmetic(self):
        """Coordinates must agree with hachoir's, or raw-vs-jpeg falsely differs."""
        jpg, _ = self._pair()
        mine = main.image_metadata(jpg)
        theirs = main.embedded_metadata(jpg)
        if not main.HACHOIR_AVAILABLE or "Latitude" not in theirs:
            self.skipTest("hachoir did not report coordinates")
        self.assertEqual(mine["Latitude"], theirs["Latitude"])
        self.assertEqual(mine["Longitude"], theirs["Longitude"])
        self.assertEqual(mine["Altitude"], theirs["Altitude"])

    def test_overlapping_keys_use_hachoirs_spelling(self):
        """Shared concepts must not appear twice under two different names."""
        jpg, _ = self._pair()
        merged = collect_metadata(jpg)["Embedded metadata"]
        for canonical in ("Model", "Make", "F number", "Exposure time"):
            self.assertNotIn(
                canonical, merged, f"{canonical} duplicates a hachoir-named key"
            )

    def test_no_property_appears_under_two_spellings(self):
        """'Aperture' and 'Aperture value' are one fact, so one row."""
        jpg, _ = self._pair()
        merged = collect_metadata(jpg)["Embedded metadata"]
        checked = 0
        for tag, alias in main.HACHOIR_KEY_ALIASES.items():
            humanized = main.humanize_tag(tag)
            if humanized == alias or alias not in merged:
                continue
            checked += 1
            self.assertNotIn(
                humanized,
                merged,
                f"{alias!r} and {humanized!r} are the same property",
            )
        self.assertGreater(checked, 5, "the fixture exercised too few aliases")

    def test_aliased_rationals_use_three_significant_figures(self):
        """hachoir renders EXIF rationals with %.3g; shared keys must match."""
        jpg, _ = self._pair()
        mine = main.image_metadata(jpg)
        self.assertEqual(mine["Shutter speed"], "4.91")
        self.assertEqual(mine["Camera brightness"], "-0.437")
        self.assertEqual(mine["Aperture"], "1.7")
        self.assertEqual(mine["Camera focal"], "1.78")
        self.assertEqual(mine["Focal length"], "6.86")

    def test_shared_keys_agree_with_hachoir(self):
        """A key both extractors report must not be formatted two ways."""
        if not main.HACHOIR_AVAILABLE:
            self.skipTest("hachoir not installed")
        jpg, _ = self._pair()
        mine = main.image_metadata(jpg)
        theirs = main.embedded_metadata(jpg)
        # hachoir applies %.3g only when the encoder stored the tag as an EXIF
        # rational. Real cameras do; Pillow's writer does not, so these two
        # legitimately disagree on this synthetic fixture while matching on
        # real camera files.
        encoder_dependent = {"Shutter speed", "Camera brightness"}
        overlap = (set(mine) & set(theirs)) - encoder_dependent
        self.assertTrue(overlap, "no overlapping keys to compare")
        for key in sorted(overlap):
            with self.subTest(key=key):
                self.assertEqual(
                    mine[key], theirs[key], f"{key} formatted two ways"
                )

    def test_non_image_falls_through_to_hachoir(self):
        wav = self.write_wav()
        self.assertEqual(main.image_metadata(wav), OrderedDict())
        merged = collect_metadata(wav)["Embedded metadata"]
        if main.HACHOIR_AVAILABLE:
            self.assertIn("MIME type", merged)

    def test_unreadable_image_does_not_raise(self):
        broken = self.write("truncated.jpg", b"\xff\xd8\xff\xe0 not really a jpeg")
        self.assertEqual(main.image_metadata(broken), OrderedDict())

    def test_no_file_handle_is_left_open(self):
        """A leaked handle would keep a Windows lock on every file compared."""
        target = self.write("notanimage.bin", b"\x00" * 64)
        self.assertEqual(main.image_metadata(target), OrderedDict())
        os.remove(target)  # PermissionError on Windows if a handle survived
        self.assertFalse(os.path.exists(target))

    def test_no_file_handle_is_left_open_on_a_real_image(self):
        jpg, _ = self._pair()
        self.assertTrue(main.image_metadata(jpg))
        os.remove(jpg)
        self.assertFalse(os.path.exists(jpg))


class FitTextTests(unittest.TestCase):
    """Text fitting is measured in pixels; a fake measurer keeps this headless."""

    @staticmethod
    def measure(text):
        return len(text) * 10

    def test_short_text_is_untouched(self):
        self.assertEqual(fit_end("abc", self.measure, 100), "abc")
        self.assertEqual(fit_middle("abc", self.measure, 100), "abc")

    def test_fit_end_truncates_to_budget(self):
        result = fit_end("abcdefghijklmnop", self.measure, 50)
        self.assertLessEqual(self.measure(result), 50)
        self.assertTrue(result.endswith("…"))
        self.assertTrue(result.startswith("abcd"))

    def test_fit_middle_keeps_both_ends(self):
        result = fit_middle(r"C:\Users\someone\Documents\deep\file.txt", self.measure, 100)
        self.assertLessEqual(self.measure(result), 100)
        self.assertTrue(result.startswith("C:"))
        self.assertTrue(result.endswith(".txt"))

    def test_impossible_budget_degrades_to_ellipsis(self):
        self.assertEqual(fit_end("abcdef", self.measure, 1), "…")
        self.assertEqual(fit_middle("abcdef", self.measure, 1), "…")


if __name__ == "__main__":
    unittest.main()
