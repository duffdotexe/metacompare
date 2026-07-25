import os
import tempfile
import unittest
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
    human_size,
    summarize,
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


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def _write(self, name, data):
        path = os.path.join(self.dir.name, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_identical_content_same_hashes(self):
        p1 = self._write("a.txt", b"hello world")
        p2 = self._write("b.txt", b"hello world")
        rows = compare_metadata(collect_metadata(p1), collect_metadata(p2))
        by_key = {key: status for _s, key, _v1, _v2, status in rows}
        self.assertEqual(by_key["SHA-256"], STATUS_SAME)
        self.assertEqual(by_key["MD5"], STATUS_SAME)
        self.assertEqual(by_key["Size"], STATUS_SAME)
        self.assertEqual(by_key["Name"], STATUS_DIFF)

    def test_different_content_different_hashes(self):
        p1 = self._write("a.txt", b"hello")
        p2 = self._write("b.txt", b"goodbye")
        rows = compare_metadata(collect_metadata(p1), collect_metadata(p2))
        by_key = {key: status for _s, key, _v1, _v2, status in rows}
        self.assertEqual(by_key["SHA-256"], STATUS_DIFF)
        self.assertEqual(by_key["Size"], STATUS_DIFF)

    def test_report_contains_paths_and_keys(self):
        p1 = self._write("a.txt", b"x")
        p2 = self._write("b.txt", b"y")
        rows = compare_metadata(collect_metadata(p1), collect_metadata(p2))
        report = build_report(p1, p2, rows)
        self.assertIn(p1, report)
        self.assertIn(p2, report)
        self.assertIn("SHA-256", report)
        self.assertIn("Summary:", report)

    def test_embedded_metadata_on_png(self):
        # Minimal 1x1 PNG — hachoir should read at least width/height.
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d4944415478da63fcffff3f030005fe02fea75481840000000049454e44ae426082"
        )
        p1 = self._write("one.png", png)
        meta = main.embedded_metadata(p1)
        if main.HACHOIR_AVAILABLE:
            self.assertTrue(
                any("width" in k.lower() for k in meta),
                f"expected image width in embedded metadata, got: {list(meta)}",
            )


if __name__ == "__main__":
    unittest.main()
