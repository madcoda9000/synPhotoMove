import datetime
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import synphotomove as spm  # noqa: E402

FAKE_EXIFTOOL = os.path.join(HERE, "fake_exiftool.py")


class FilenameDateTest(unittest.TestCase):
    def check(self, name, expected):
        self.assertEqual(spm.date_from_filename(name), expected, name)

    def test_patterns(self):
        dt = datetime.datetime
        self.check("20240501_191127.jpg", dt(2024, 5, 1, 19, 11, 27))
        self.check("20240501_195612~2_U1715943544436798019.jpg", dt(2024, 5, 1, 19, 56, 12))
        self.check("IMG-20240526-WA0027.jpg", dt(2024, 5, 26))
        self.check("IMG_20240315_123456.jpg", dt(2024, 3, 15, 12, 34, 56))
        self.check("PXL_20240315_123456789.jpg", dt(2024, 3, 15, 12, 34, 56))
        self.check("IMG_1282_20230723130356112.HEIC", dt(2023, 7, 23, 13, 3, 56))
        self.check("2024-03-15 12.34.56.jpg", dt(2024, 3, 15, 12, 34, 56))
        self.check("WhatsApp Image 2024-03-15 at 12.34.56.jpeg", dt(2024, 3, 15, 12, 34, 56))
        self.check("Screenshot_2024-03-15-12-34-56-123_com.app.png", dt(2024, 3, 15, 12, 34, 56))
        self.check("signal-2023-12-24-180000.jpg", dt(2023, 12, 24, 18, 0, 0))
        self.check("Urlaub 24.12.2023.jpg", dt(2023, 12, 24))
        self.check("20240501053902.jpg", dt(2024, 5, 1, 5, 39, 2))
        self.check("FB_IMG_1707866479342.jpg", dt.fromtimestamp(1707866479))
        self.check("IMG_1729937988349_oviva.jpg", dt.fromtimestamp(1729937988))

    def test_no_date(self):
        for name in ("IMG_3660.PNG", "21351C94-3030-43F0-AB53-4292F994BD8E.png",
                     "53446e5f-123f-4b7f-a723-eb06f63486a3_U1770845165532534872.mp4",
                     "Dein Abschnittstext (Postkarten).PNG", "IMG_20241399_000000.jpg",
                     "20990101_120000.jpg", "DSC_1234.JPG", "4158ar7ttppvup0c6lfj0sqx9 (1).jpg",
                     "file_000000009d6061f4b6d36cc618d4f01e.png", "FB_IMG_9999999999999.jpg"):
            self.check(name, None)


class MetaDateTest(unittest.TestCase):
    def test_parse(self):
        p = spm.parse_meta_date
        self.assertEqual(p("2024:05:01 12:34:56"), datetime.datetime(2024, 5, 1, 12, 34, 56))
        self.assertEqual(p("2024:05:01 05:39:02+02:00"), datetime.datetime(2024, 5, 1, 5, 39, 2))
        self.assertEqual(p("2024:05:01"), datetime.datetime(2024, 5, 1))
        self.assertIsNone(p("0000:00:00 00:00:00"))
        self.assertIsNone(p("    :  :     :  :  "))
        self.assertIsNone(p(None))

    def test_priority(self):
        date, tag = spm.date_from_metadata({"CreateDate": "2020:01:01 00:00:00",
                                            "DateTimeOriginal": "2019:02:03 04:05:06"})
        self.assertEqual((date.year, tag), (2019, "DateTimeOriginal"))
        date, tag = spm.date_from_metadata({"DateTimeOriginal": "0000:00:00 00:00:00",
                                            "CreateDate": "2020:01:01 00:00:00"})
        self.assertEqual((date.year, tag), (2020, "CreateDate"))


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.target = os.path.join(self.base, "photo")
        self.upload = os.path.join(self.target, "upload_alice")
        self.exif = {}
        os.makedirs(self.upload)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, content=None, root=None, exif=None):
        path = os.path.join(root or self.upload, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content if content is not None else rel.encode())
        if exif is not None:
            self.exif[os.path.basename(rel)] = exif
        return path

    def run_tool(self, *extra, min_age=0):
        exif_file = os.path.join(self.base, "exif.json")
        with open(exif_file, "w") as fh:
            json.dump(self.exif, fh)
        os.environ["FAKE_EXIF"] = exif_file
        fake = os.path.join(self.base, "exiftool")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, FAKE_EXIFTOOL))
        os.chmod(fake, 0o755)
        cfg = os.path.join(self.base, "test.ini")
        with open(cfg, "w") as fh:
            fh.write("[general]\ntarget = %s\nexiftool = %s\nmin_age_minutes = %s\n"
                     "lock_file = %s\n[sources]\nalice = %s\n"
                     % (self.target, fake, min_age, os.path.join(self.base, "lock"), self.upload))
        spm.log.handlers[:] = []
        return spm.main(["-c", cfg] + list(extra))

    def exists(self, rel, root=None):
        return os.path.exists(os.path.join(root or self.target, rel))

    def read(self, rel):
        with open(os.path.join(self.target, rel), "rb") as fh:
            return fh.read()

    def test_full_run(self):
        exif = {"DateTimeOriginal": "2024:05:01 12:00:00"}
        self.write("DCIM/a.jpg", exif=exif)
        self.write("DCIM/@eaDir/a.jpg/SYNOPHOTO_THUMB_S.jpg")
        self.write("IMG-20240526-WA0027.jpg", exif={})
        self.write("random.png", exif={})
        # Namenskonflikt mit anderem Inhalt
        self.write("2024/05/b.jpg", b"anders", root=self.target)
        self.write("b.jpg", exif=exif)
        # Inhaltsgleiches Duplikat
        self.write("2024/05/c.jpg", b"gleich", root=self.target)
        self.write("c.jpg", b"gleich", exif=exif)
        # Live Photo mit Begleitdatei, HEIC-Name im Ziel schon belegt
        self.write("2024/05/IMG_1.HEIC", b"fremd", root=self.target)
        self.write("live/IMG_1.HEIC", exif=exif)
        self.write("live/IMG_1.MOV", exif={"CreationDate": "2024:05:01 12:00:01+02:00"})
        self.write("live/IMG_1.AAE")
        # Ausgeschlossene Ordner und nicht unterstützte Dateien
        self.write("error/x.jpg", exif=exif)
        self.write("#recycle/y.jpg", exif=exif)
        self.write("notes.txt")
        # Groß-/Kleinschreibung: d.JPG gegen vorhandenes D.jpg
        self.write("2024/05/D.jpg", b"anders", root=self.target)
        self.write("d.JPG", exif=exif)

        self.assertEqual(self.run_tool(), 0)

        self.assertEqual(self.read("2024/05/a.jpg"), b"DCIM/a.jpg")
        self.assertFalse(self.exists("DCIM/a.jpg", self.upload))
        self.assertFalse(self.exists("DCIM/@eaDir/a.jpg", self.upload))
        self.assertTrue(self.exists("2024/05/IMG-20240526-WA0027.jpg"))
        self.assertTrue(self.exists("__noDateInformation/random.png", self.upload))
        self.assertEqual(self.read("2024/05/b.jpg"), b"anders")
        self.assertEqual(self.read("2024/05/b_1.jpg"), b"b.jpg")
        self.assertTrue(self.exists("duplicate/c.jpg", self.upload))
        self.assertFalse(self.exists("c.jpg", self.upload))
        for name in ("IMG_1_1.HEIC", "IMG_1_1.MOV", "IMG_1_1.AAE"):
            self.assertTrue(self.exists("2024/05/" + name), name)
        self.assertFalse(self.exists("2024/05/IMG_1.MOV"))
        self.assertTrue(self.exists("error/x.jpg", self.upload))
        self.assertTrue(self.exists("#recycle/y.jpg", self.upload))
        self.assertTrue(self.exists("notes.txt", self.upload))
        self.assertTrue(self.exists("2024/05/d_1.JPG"))
        self.assertTrue(self.exists("DCIM", self.upload))  # leere Ordner bleiben standardmäßig

        logs = os.listdir(os.path.join(self.upload, "logs"))
        self.assertEqual(len(logs), 1)
        leftovers = [f for _, _, fs in os.walk(self.target) for f in fs if f.endswith(spm.TMP_SUFFIX)]
        self.assertEqual(leftovers, [])

    def test_dry_run_changes_nothing(self):
        self.write("DCIM/a.jpg", exif={"DateTimeOriginal": "2024:05:01 12:00:00"})
        self.write("random.png", exif={})
        before = sorted(os.path.join(d, f) for d, _, fs in os.walk(self.base) for f in fs)
        self.assertEqual(self.run_tool("--dry-run"), 0)
        after = sorted(os.path.join(d, f) for d, _, fs in os.walk(self.base) for f in fs
                       if f not in ("exif.json", "exiftool", "test.ini", "lock"))
        before = [p for p in before if os.path.basename(p) not in ("exif.json", "exiftool", "test.ini", "lock")]
        self.assertEqual(before, after)
        self.assertFalse(self.exists("2024"))

    def test_young_files_are_skipped(self):
        self.write("a.jpg", exif={"DateTimeOriginal": "2024:05:01 12:00:00"})
        self.assertEqual(self.run_tool(min_age=10), 0)
        self.assertTrue(self.exists("a.jpg", self.upload))

    def test_missing_exiftool_answer_keeps_file(self):
        self.write("a.jpg", exif="OMIT")
        self.assertEqual(self.run_tool(), 1)
        self.assertTrue(self.exists("a.jpg", self.upload))
        self.assertFalse(self.exists("__noDateInformation", self.upload))

    def test_same_name_twice_in_one_run(self):
        exif = {"DateTimeOriginal": "2024:05:01 12:00:00"}
        self.write("one/a.jpg", b"eins", exif=exif)
        self.exif["a.jpg"] = exif
        self.write("two/a.jpg", b"zwei")
        self.assertEqual(self.run_tool(), 0)
        self.assertEqual({self.read("2024/05/a.jpg"), self.read("2024/05/a_1.jpg")}, {b"eins", b"zwei"})

    def test_remove_empty_dirs(self):
        self.write("DCIM/Camera/a.jpg", exif={"DateTimeOriginal": "2024:05:01 12:00:00"})
        self.write("DCIM/Camera/@eaDir/a.jpg/thumb.jpg")
        self.assertEqual(self.run_tool("--remove-empty-dirs"), 0)
        self.assertFalse(self.exists("DCIM", self.upload))
        self.assertTrue(self.exists("logs", self.upload))


if __name__ == "__main__":
    unittest.main()
