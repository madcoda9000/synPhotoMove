#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
synPhotoMove – sortiert Fotos und Videos aus Upload-Ordnern anhand des
Aufnahmedatums in die Fotoablage (Ziel/JJJJ/MM).

Gedacht für den Aufgabenplaner einer Synology (DSM 7, Python >= 3.8).
Benötigt exiftool. Bedienung und Konfiguration: siehe README.md.
"""

import argparse
import configparser
import datetime
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple

VERSION = "1.0.0"

DEFAULT_IMAGE_EXT = ("jpg jpeg jpe heic heif hif avif png webp gif tif tiff bmp "
                     "dng cr2 cr3 crw nef nrw arw srf sr2 orf rw2 raf pef srw x3f 3fr erf mrw raw")
DEFAULT_VIDEO_EXT = "mp4 mov m4v 3gp 3g2 mts m2ts avi mkv wmv mpg mpeg"
DEFAULT_SIDECAR_EXT = "aae xmp thm"

# Synology-Systemordner, die nie durchsucht werden
ALWAYS_EXCLUDED_DIRS = {"@eadir", "#recycle", "#snapshot"}

# Reihenfolge = Priorität. CreationDate (QuickTime Keys) enthält bei iPhone-Videos
# die lokale Aufnahmezeit, CreateDate/MediaCreateDate sind bei Videos UTC und
# werden durch "-api QuickTimeUTC" von exiftool in lokale Zeit umgerechnet.
DATE_TAGS = ["DateTimeOriginal", "CreationDate", "CreateDate", "MediaCreateDate",
             "TrackCreateDate", "DateCreated", "DateTimeCreated"]

TMP_SUFFIX = ".synphotomove-tmp"
EXIFTOOL_CHUNK = 500
MAX_RENAME = 10000
LOG_PREFIX = "synphotomove_"

log = logging.getLogger("synphotomove")


class ConfigError(Exception):
    """Fehler in der Konfigurationsdatei oder den Aufrufparametern.

    Führt in main() zum Abbruch mit Exit-Code 2.
    """


class ExiftoolError(Exception):
    """exiftool konnte nicht gestartet werden oder lieferte keine lesbare Ausgabe.

    Die betroffene Quelle wird in diesem Lauf übersprungen.
    """


# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #

class Config:
    """Alle Einstellungen eines Laufs, zusammengeführt aus INI-Datei und Kommandozeile.

    Attributes:
        target: Wurzel der Fotoablage, z.B. /volume1/photo.
        sources: Quellname -> Pfad des Upload-Ordners.
        folder_format: Unterordner im Ziel, Platzhalter {year}, {month}, {day}.
        exiftool: Pfad zu exiftool.
        min_age_minutes: Jüngere Dateien werden übersprungen (Upload läuft evtl. noch).
        media_ext: Endungen (klein, ohne Punkt), die einsortiert werden.
        sidecar_ext: Endungen der Begleitdateien (.aae, .xmp, …).
        image_ext: Endungen der Bilder; Begleitdateien gehören bevorzugt zu einem Bild.
        nodate_folder: Ordner im Upload-Ordner für Dateien ohne Datum.
        duplicate_folder: Ordner im Upload-Ordner für inhaltsgleiche Duplikate.
        log_folder: Ordner im Upload-Ordner für die Logdateien.
        log_keep_months: So viele Monate werden Logdateien aufbewahrt.
        exclude_dirs: Ordnernamen (klein), die nicht durchsucht werden.
        remove_empty_dirs: Leere Unterordner nach dem Lauf löschen.
        lock_file: Sperrdatei gegen parallele Läufe.
        dry_run: Testmodus, es wird nichts verändert.
    """

    def __init__(self):
        """Belegt alle Einstellungen mit ihren Standardwerten."""
        self.target = ""
        self.sources = {}                     # type: Dict[str, str]
        self.folder_format = "{year}/{month}"
        self.exiftool = "/usr/local/bin/exiftool"
        self.min_age_minutes = 10.0
        self.media_ext = frozenset()
        self.sidecar_ext = frozenset()
        self.image_ext = frozenset()
        self.nodate_folder = "__noDateInformation"
        self.duplicate_folder = "duplicate"
        self.log_folder = "logs"
        self.log_keep_months = 12
        self.exclude_dirs = frozenset()
        self.remove_empty_dirs = False
        self.lock_file = os.path.join(tempfile.gettempdir(), "synphotomove.lock")
        self.dry_run = False


def _ext_set(value):
    """Wandelt eine Liste von Dateiendungen in eine Menge um.

    Args:
        value: Endungen, getrennt durch Leerzeichen, Komma oder Semikolon,
            mit oder ohne Punkt, z.B. "jpg .HEIC, png".

    Returns:
        frozenset der Endungen in Kleinbuchstaben ohne Punkt.
    """
    return frozenset(e.strip().lstrip(".").lower() for e in re.split(r"[\s,;]+", value) if e.strip())


def _subfolder(value, option):
    """Prüft einen Ordnernamen relativ zum Upload-Ordner.

    Args:
        value: Wert aus der Konfiguration, z.B. "duplicate" oder "/logs/".
        option: Name der Option, nur für die Fehlermeldung.

    Returns:
        Den Ordnernamen ohne führende und abschließende Schrägstriche.

    Raises:
        ConfigError: Der Wert ist leer oder enthält "..".
    """
    value = value.strip().strip("/")
    if not value or ".." in value.split("/"):
        raise ConfigError("Ungültiger Wert für %s: %r" % (option, value))
    return value


def load_config(args):
    """Liest die INI-Datei, ergänzt sie um die Kommandozeile und prüft das Ergebnis.

    Kommandozeilenoptionen haben Vorrang vor der INI-Datei. Geprüft wird, dass
    Ziel und Quellen angegeben sind, das Ziel existiert und nicht innerhalb einer
    Quelle liegt, folder_format gültig ist und exiftool gefunden wird.

    Args:
        args: Ergebnis von parse_args().

    Returns:
        Die fertige Config.

    Raises:
        ConfigError: Datei nicht lesbar, Wert ungültig oder Prüfung fehlgeschlagen.
    """
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # Quellnamen nicht in Kleinbuchstaben umwandeln
    if args.config:
        if not cp.read(args.config, encoding="utf-8"):
            raise ConfigError("Konfigurationsdatei nicht lesbar: %s" % args.config)
    g = cp["general"] if cp.has_section("general") else {}

    cfg = Config()
    try:
        cfg.target = args.target or g.get("target", "")
        cfg.folder_format = g.get("folder_format", cfg.folder_format)
        cfg.exiftool = g.get("exiftool", cfg.exiftool)
        cfg.min_age_minutes = float(args.min_age if args.min_age is not None
                                    else g.get("min_age_minutes", cfg.min_age_minutes))
        include_videos = _bool(g.get("include_videos", "true"))
        cfg.image_ext = _ext_set(g.get("image_extensions", DEFAULT_IMAGE_EXT))
        video_ext = _ext_set(g.get("video_extensions", DEFAULT_VIDEO_EXT))
        cfg.media_ext = cfg.image_ext | (video_ext if include_videos else frozenset())
        cfg.sidecar_ext = _ext_set(g.get("sidecar_extensions", DEFAULT_SIDECAR_EXT)) - cfg.media_ext
        cfg.nodate_folder = _subfolder(g.get("nodate_folder", cfg.nodate_folder), "nodate_folder")
        cfg.duplicate_folder = _subfolder(g.get("duplicate_folder", cfg.duplicate_folder), "duplicate_folder")
        cfg.log_folder = _subfolder(g.get("log_folder", cfg.log_folder), "log_folder")
        cfg.log_keep_months = int(g.get("log_keep_months", cfg.log_keep_months))
        cfg.remove_empty_dirs = args.remove_empty_dirs or _bool(g.get("remove_empty_dirs", "false"))
        cfg.lock_file = g.get("lock_file", cfg.lock_file)
        cfg.dry_run = args.dry_run or _bool(g.get("dry_run", "false"))
        extra = {d.strip().lower() for d in g.get("exclude_dirs", "error").split(",") if d.strip()}
    except ValueError as e:
        raise ConfigError("Ungültiger Wert in [general]: %s" % e)

    # Die eigenen Ordner im Upload-Ordner dürfen nie selbst wieder durchsucht werden
    cfg.exclude_dirs = frozenset(extra | {cfg.nodate_folder.split("/")[0].lower(),
                                          cfg.duplicate_folder.split("/")[0].lower(),
                                          cfg.log_folder.split("/")[0].lower()})

    if cp.has_section("sources"):
        cfg.sources = {k: v.strip() for k, v in cp["sources"].items() if v.strip()}
    for i, src in enumerate(args.source or []):
        cfg.sources[os.path.basename(src.rstrip("/")) or "source%d" % i] = src

    if not cfg.target:
        raise ConfigError("Kein Ziel angegeben (target in [general] oder --target).")
    if not os.path.isdir(cfg.target):
        raise ConfigError("Zielordner existiert nicht: %s" % cfg.target)
    if not cfg.sources:
        raise ConfigError("Keine Quellen angegeben ([sources] oder --source).")
    try:
        target_path(cfg, datetime.datetime(2000, 1, 2))
    except (KeyError, IndexError, ValueError) as e:
        raise ConfigError("Ungültiges folder_format %r: %s" % (cfg.folder_format, e))
    if not (os.path.isfile(cfg.exiftool) and os.access(cfg.exiftool, os.X_OK)):
        found = shutil.which(cfg.exiftool)
        if not found:
            raise ConfigError("exiftool nicht gefunden: %s" % cfg.exiftool)
        cfg.exiftool = found

    target_real = os.path.realpath(cfg.target)
    for name, src in cfg.sources.items():
        src_real = os.path.realpath(src)
        if os.path.commonpath([target_real, src_real]) == src_real:
            raise ConfigError("Ziel %s liegt innerhalb der Quelle %s (%s)." % (cfg.target, src, name))
    return cfg


def _bool(value):
    """Wandelt einen Konfigurationswert in einen Wahrheitswert um.

    Args:
        value: z.B. "true", "ja", "1", "off" (Groß-/Kleinschreibung egal).

    Returns:
        True oder False; ein leerer Wert gilt als False.

    Raises:
        ValueError: Der Wert ist kein bekannter Wahrheitswert.
    """
    v = str(value).strip().lower()
    if v in ("1", "true", "yes", "ja", "on"):
        return True
    if v in ("0", "false", "no", "nein", "off", ""):
        return False
    raise ValueError("kein Wahrheitswert: %r" % value)


def target_path(cfg, date):
    """Berechnet den Zielordner für ein Aufnahmedatum.

    Args:
        cfg: Konfiguration mit target und folder_format.
        date: Aufnahmedatum.

    Returns:
        Absoluter Ordnerpfad, z.B. /volume1/photo/2024/05.

    Raises:
        KeyError, IndexError, ValueError: folder_format ist ungültig.
    """
    rel = cfg.folder_format.format(year="%04d" % date.year, month="%02d" % date.month,
                                   day="%02d" % date.day)
    return os.path.join(cfg.target, *[p for p in rel.split("/") if p])


# --------------------------------------------------------------------------- #
# Datum ermitteln
# --------------------------------------------------------------------------- #

_META_DATE = re.compile(r"^\s*(\d{4})[:\-.](\d{2})[:\-.](\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?")

# JJJJMMTT / JJJJ-MM-TT / JJJJ_MM_TT / JJJJ.MM.TT, optional mit Uhrzeit (+ Millisekunden), z.B.
# 20240501_191127, IMG-20240526-WA0027, PXL_20240315_123456789, 2024-03-15 12.34.56,
# "WhatsApp Image 2024-03-15 at 12.34.56", Screenshot_2024-03-15-12-34-56
_FILENAME_YMD = re.compile(
    r"(?<!\d)(?P<y>19\d\d|20\d\d)(?P<sep>[-_.]?)(?P<m>0[1-9]|1[0-2])(?P=sep)(?P<d>0[1-9]|[12]\d|3[01])"
    r"(?:[-_. T]?(?:at[ _])?(?P<H>[01]\d|2[0-3])[-_.:h]?(?P<M>[0-5]\d)[-_.:m]?(?P<S>[0-5]\d)\d{0,3})?(?!\d)")
# TT.MM.JJJJ / TT-MM-JJJJ / TT_MM_JJJJ
_FILENAME_DMY = re.compile(
    r"(?<!\d)(?P<d>0[1-9]|[12]\d|3[01])(?P<sep>[-_.])(?P<m>0[1-9]|1[0-2])(?P=sep)(?P<y>19\d\d|20\d\d)(?!\d)")

# Unix-Zeitstempel in Millisekunden, z.B. FB_IMG_1707866479342, IMG_1729937988349_oviva
_FILENAME_EPOCH_MS = re.compile(r"(?<!\d)(1\d{12})(?!\d)")

META_MIN_YEAR = 1900
FILENAME_MIN_YEAR = 1990
EPOCH_MIN_YEAR = 2005


def _plausible(date, min_year):
    """Prüft, ob ein Datum als Aufnahmedatum in Frage kommt.

    Args:
        date: Das zu prüfende Datum.
        min_year: Frühestes erlaubtes Jahr.

    Returns:
        True, wenn das Jahr >= min_year ist und das Datum höchstens einen Tag
        in der Zukunft liegt.
    """
    return date.year >= min_year and date <= datetime.datetime.now() + datetime.timedelta(days=1)


def parse_meta_date(value):
    # type: (object) -> Optional[datetime.datetime]
    """Liest ein Datum aus einem exiftool-Wert.

    Eine Zeitzonenangabe (z.B. "+02:00") wird ignoriert, die Uhrzeit bleibt
    so, wie sie im Wert steht.

    Args:
        value: Wert eines Datums-Tags, z.B. "2024:05:01 12:34:56+02:00".

    Returns:
        Das Datum, oder None, wenn der Wert fehlt, kein Datum ist (z.B.
        "0000:00:00 00:00:00") oder nicht plausibel ist.
    """
    if not isinstance(value, str):
        return None
    m = _META_DATE.match(value)
    if not m:
        return None
    try:
        date = datetime.datetime(*(int(x) for x in m.groups(default="0")))
    except ValueError:
        return None
    return date if _plausible(date, META_MIN_YEAR) else None


def date_from_metadata(entry):
    # type: (dict) -> Tuple[Optional[datetime.datetime], str]
    """Sucht das Aufnahmedatum in den Metadaten einer Datei.

    Die Tags werden in der Reihenfolge von DATE_TAGS geprüft, das erste
    gültige Datum gewinnt.

    Args:
        entry: Eintrag einer Datei aus der JSON-Ausgabe von exiftool.

    Returns:
        (Datum, Name des Tags), oder (None, "") ohne gültiges Datum.
    """
    for tag in DATE_TAGS:
        date = parse_meta_date(entry.get(tag))
        if date:
            return date, tag
    return None, ""


def date_from_filename(name):
    # type: (str) -> Optional[datetime.datetime]
    """Sucht ein Datum im Dateinamen.

    Reihenfolge: JJJJMMTT (optional mit Uhrzeit), dann TT.MM.JJJJ, zuletzt ein
    13-stelliger Unix-Zeitstempel in Millisekunden.

    Args:
        name: Dateiname mit oder ohne Endung, z.B. "IMG-20240526-WA0027.jpg".

    Returns:
        Das erste plausible Datum, oder None.
    """
    stem = os.path.splitext(name)[0]
    for regex in (_FILENAME_YMD, _FILENAME_DMY):
        for m in regex.finditer(stem):
            g = m.groupdict()
            try:
                date = datetime.datetime(int(g["y"]), int(g["m"]), int(g["d"]))
            except ValueError:
                continue
            if g.get("H"):
                date = date.replace(hour=int(g["H"]), minute=int(g["M"]), second=int(g["S"]))
            if _plausible(date, FILENAME_MIN_YEAR):
                return date
    for m in _FILENAME_EPOCH_MS.finditer(stem):
        date = datetime.datetime.fromtimestamp(int(m.group(1)) / 1000.0).replace(microsecond=0)
        if _plausible(date, EPOCH_MIN_YEAR):
            return date
    return None


def run_exiftool(exiftool, paths):
    # type: (str, List[str]) -> Dict[str, dict]
    """Liest die Datums-Tags aller Dateien mit möglichst wenigen exiftool-Aufrufen.

    Die Pfade werden in Paketen zu EXIFTOOL_CHUNK Dateien über eine
    Argumentdatei übergeben, damit die Kommandozeile nicht zu lang wird.

    Args:
        exiftool: Pfad zu exiftool.
        paths: Die zu lesenden Dateien.

    Returns:
        Pfad -> exiftool-Eintrag. Dateien, zu denen exiftool nichts liefert,
        fehlen im Ergebnis.

    Raises:
        ExiftoolError: exiftool nicht startbar, Zeitüberschreitung, Fehler ohne
            Ausgabe oder Ausgabe kein gültiges JSON.
    """
    results = {}  # type: Dict[str, dict]
    cmd = [exiftool, "-json", "-charset", "filename=utf8", "-api", "QuickTimeUTC=1"]
    cmd += ["-" + t for t in DATE_TAGS]
    for start in range(0, len(paths), EXIFTOOL_CHUNK):
        chunk = paths[start:start + EXIFTOOL_CHUNK]
        fd, argfile = tempfile.mkstemp(prefix="synphotomove-", suffix=".args")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as fh:
                fh.write("\n".join(chunk) + "\n")
            try:
                proc = subprocess.run(cmd + ["-@", argfile], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=3600)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise ExiftoolError(str(e))
        finally:
            os.remove(argfile)
        out = proc.stdout.decode("utf-8", "surrogateescape").strip()
        if not out:
            # exiftool liefert Exit-Code 1, wenn einzelne Dateien fehlerhaft sind –
            # das ist nur dann ein Problem, wenn gar nichts zurückkommt
            if proc.returncode != 0:
                raise ExiftoolError(proc.stderr.decode("utf-8", "replace").strip()
                                    or "Exit-Code %d" % proc.returncode)
            continue
        try:
            entries = json.loads(out)
        except ValueError as e:
            raise ExiftoolError("Ausgabe nicht lesbar: %s" % e)
        for entry in entries:
            results[entry.get("SourceFile", "")] = entry
    return results


# --------------------------------------------------------------------------- #
# Dateioperationen
# --------------------------------------------------------------------------- #

def sha256_file(path):
    """Berechnet die SHA-256-Prüfsumme einer Datei.

    Args:
        path: Pfad der Datei.

    Returns:
        Prüfsumme als Hex-String.

    Raises:
        OSError: Datei nicht lesbar.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def copy_verified(src, dst):
    """Kopiert src nach dst über eine temporäre Datei und prüft Größe und SHA-256.

    dst taucht erst auf, wenn die Kopie vollständig und geprüft ist. Bei einem
    Fehler (auch Strg+C) wird die temporäre Datei wieder gelöscht, src bleibt
    unverändert.

    Args:
        src: Quelldatei.
        dst: Zieldatei; darf noch nicht existieren, der Ordner muss existieren.

    Raises:
        OSError: Kopieren fehlgeschlagen, Prüfung fehlgeschlagen oder dst ist
            zwischenzeitlich entstanden.
    """
    tmp = os.path.join(os.path.dirname(dst), "." + os.path.basename(dst) + TMP_SUFFIX)
    if os.path.lexists(tmp):
        os.remove(tmp)  # Rest eines abgebrochenen Laufs
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(tmp, "xb") as fo:
            for block in iter(lambda: fi.read(1 << 20), b""):
                h.update(block)
                fo.write(block)
            fo.flush()
            os.fsync(fo.fileno())
        try:
            shutil.copystat(src, tmp)
        except OSError:
            pass  # Zeitstempel sind nett, aber nicht entscheidend
        size = os.path.getsize(src)
        if os.path.getsize(tmp) != size:
            raise IOError("Größe der Kopie stimmt nicht")
        if sha256_file(tmp) != h.hexdigest():
            raise IOError("Prüfsumme der Kopie stimmt nicht")
        if os.path.lexists(dst):
            raise IOError("Zieldatei ist zwischenzeitlich entstanden: %s" % dst)
        os.rename(tmp, dst)
    except BaseException:
        if os.path.lexists(tmp):
            os.remove(tmp)
        raise
    if not os.path.isfile(dst) or os.path.getsize(dst) != size:
        raise IOError("Zieldatei nach dem Kopieren nicht vorhanden: %s" % dst)


def remove_source(path):
    """Löscht eine Quelldatei samt ihrer Synology-Vorschaubilder in @eaDir.

    Args:
        path: Die zu löschende Datei.

    Raises:
        OSError: Die Datei selbst konnte nicht gelöscht werden.
    """
    os.remove(path)
    # Synology-Vorschaubilder der gelöschten Datei mit entfernen
    thumbs = os.path.join(os.path.dirname(path), "@eaDir", os.path.basename(path))
    if os.path.isdir(thumbs):
        shutil.rmtree(thumbs, ignore_errors=True)


class FileCompare:
    """Inhaltsvergleich mit zwischengespeicherten Prüfsummen."""

    def __init__(self):
        """Legt einen leeren Cache an (Pfad, Größe, mtime) -> Prüfsumme."""
        self._cache = {}  # type: Dict[Tuple[str, int, int], str]

    def sha256(self, path):
        """Liefert die Prüfsumme einer Datei, aus dem Cache, solange sie unverändert ist.

        Args:
            path: Pfad der Datei.

        Returns:
            SHA-256 als Hex-String.

        Raises:
            OSError: Datei nicht lesbar.
        """
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime_ns)
        if key not in self._cache:
            self._cache[key] = sha256_file(path)
        return self._cache[key]

    def same(self, a, b):
        """Prüft, ob zwei Dateien denselben Inhalt haben.

        Args:
            a: Erste Datei.
            b: Zweite Datei.

        Returns:
            True bei gleicher Größe und gleicher Prüfsumme; False sonst oder
            wenn eine der Dateien nicht lesbar ist.
        """
        try:
            if os.path.getsize(a) != os.path.getsize(b):
                return False
            return self.sha256(a) == self.sha256(b)
        except OSError:
            return False


class DirIndex:
    """Dateinamen pro Zielordner, ohne Beachtung der Groß-/Kleinschreibung (wie SMB).

    Im Testmodus werden geplante Dateien eingetragen, als wären sie kopiert worden
    (der Pfad zeigt dann auf die Quelldatei, damit Inhaltsvergleiche funktionieren).
    """

    def __init__(self):
        """Legt einen leeren Index an; Ordner werden erst bei Bedarf eingelesen."""
        self._dirs = {}  # type: Dict[str, Dict[str, str]]

    def _load(self, directory):
        """Liest einen Ordner beim ersten Zugriff ein.

        Args:
            directory: Der Ordner; existiert er nicht, gilt er als leer.

        Returns:
            Dateiname in Kleinbuchstaben -> Pfad.
        """
        if directory not in self._dirs:
            try:
                names = os.listdir(directory)
            except FileNotFoundError:
                names = []
            self._dirs[directory] = {n.lower(): os.path.join(directory, n) for n in names}
        return self._dirs[directory]

    def get(self, directory, name):
        # type: (str, str) -> Optional[str]
        """Sucht einen Dateinamen in einem Ordner.

        Args:
            directory: Der Ordner.
            name: Dateiname, Groß-/Kleinschreibung egal.

        Returns:
            Pfad der vorhandenen Datei, oder None, wenn der Name frei ist.
        """
        return self._load(directory).get(name.lower())

    def add(self, directory, name, path):
        """Trägt eine (neue oder geplante) Datei ein, damit der Name als belegt gilt.

        Args:
            directory: Zielordner.
            name: Dateiname im Zielordner.
            path: Pfad, über den der Inhalt gelesen werden kann.
        """
        self._load(directory)[name.lower()] = path


# --------------------------------------------------------------------------- #
# Verarbeitung
# --------------------------------------------------------------------------- #

class Stats:
    """Zähler für einen Lauf bzw. eine Quelle.

    Attributes:
        moved: Einsortierte Mediendateien (inkl. umbenannter).
        renamed: Davon wegen eines Namenskonflikts umbenannt.
        duplicates: Nach duplicate verschobene Mediendateien.
        nodate: Nach __noDateInformation verschobene Mediendateien.
        sidecars: Mitverschobene Begleitdateien.
        young: Übersprungene, noch zu junge Dateien.
        errors: Fehler; jeder Fehler führt zu Exit-Code 1.
    """

    FIELDS = ("moved", "renamed", "duplicates", "nodate", "sidecars", "young", "errors")

    def __init__(self):
        """Setzt alle Zähler auf 0."""
        for f in self.FIELDS:
            setattr(self, f, 0)

    def add(self, other):
        """Addiert die Zähler einer anderen Statistik hinzu.

        Args:
            other: Die zu addierende Stats-Instanz.
        """
        for f in self.FIELDS:
            setattr(self, f, getattr(self, f) + getattr(other, f))

    def anything(self):
        """Prüft, ob etwas passiert ist (zu junge Dateien zählen nicht).

        Returns:
            True, wenn mindestens ein Zähler außer young größer 0 ist.
        """
        return any(getattr(self, f) for f in self.FIELDS if f != "young")

    def __str__(self):
        """Liefert die Zusammenfassung für das Log."""
        return ("%d einsortiert (davon %d umbenannt), %d ohne Datum, %d Duplikate, "
                "%d Begleitdateien, %d noch zu jung, %d Fehler"
                % (self.moved, self.renamed, self.nodate, self.duplicates,
                   self.sidecars, self.young, self.errors))


class Sidecar:
    """Begleitdatei (.AAE, .xmp, .thm), die mit ihrer Mediendatei mitwandert."""

    def __init__(self, path, tail, full):
        """
        Args:
            path: Pfad der Begleitdatei.
            tail: Endung, die an den neuen Namen angehängt wird (".AAE" / ".xmp").
            full: True, wenn sie am vollen Dateinamen hängt (IMG_1.HEIC.xmp),
                False, wenn am Stamm (IMG_1.AAE).
        """
        self.path = path
        self.name = os.path.basename(path)
        self.tail = tail  # Endung, die an den neuen Namen angehängt wird (".AAE" / ".xmp")
        self.full = full  # True: IMG_1.HEIC.xmp (hängt am Dateinamen), False: IMG_1.AAE (am Stamm)


class MediaItem:
    """Eine Foto- oder Videodatei, die einsortiert werden soll.

    Attributes:
        path: Pfad im Upload-Ordner.
        name: Dateiname.
        stem: Dateiname ohne Endung.
        ext: Endung mit Punkt, Schreibweise wie im Original.
        date: Ermitteltes Aufnahmedatum, None ohne Datum.
        how: Herkunft des Datums (Tag-Name oder "Dateiname").
        dest_dir: Zielordner.
        sidecars: Zugeordnete Begleitdateien.
    """

    def __init__(self, path):
        """
        Args:
            path: Pfad der Datei im Upload-Ordner.
        """
        self.path = path
        self.name = os.path.basename(path)
        self.stem, self.ext = os.path.splitext(self.name)
        self.date = None  # type: Optional[datetime.datetime]
        self.how = ""
        self.dest_dir = ""
        self.sidecars = []  # type: List[Sidecar]


class SourceProcessor:
    """Verarbeitet einen Upload-Ordner (eine Quelle aus [sources])."""

    def __init__(self, cfg, name, root):
        """
        Args:
            cfg: Konfiguration des Laufs.
            name: Name der Quelle, z.B. "alice" (für Logausgaben).
            root: Pfad des Upload-Ordners.
        """
        self.cfg = cfg
        self.name = name
        self.root = root
        self.stats = Stats()
        self.index = DirIndex()
        self.compare = FileCompare()
        self.dry = cfg.dry_run

    # -- Hilfen für Ausgaben ------------------------------------------------- #

    def rel(self, path):
        """Kürzt einen Pfad für das Log.

        Args:
            path: Absoluter Pfad.

        Returns:
            Pfad relativ zum Upload-Ordner oder zum Ziel; sonst unverändert.
        """
        for base in (self.root, self.cfg.target):
            try:
                r = os.path.relpath(path, base)
            except ValueError:
                continue
            if not r.startswith(".."):
                return r
        return path

    def info(self, msg, *args):
        """Schreibt eine Info-Meldung, im Testmodus mit Präfix "[TEST]".

        Args:
            msg: Formatstring für logging.
            *args: Werte für den Formatstring.
        """
        log.info(("[TEST] " if self.dry else "") + msg, *args)

    # -- Ablauf -------------------------------------------------------------- #

    def run(self):
        """Verarbeitet den gesamten Upload-Ordner.

        Durchsucht den Ordner, liest die Metadaten aller Dateien mit exiftool und
        sortiert Ordner für Ordner ein. Danach werden auf Wunsch leere Ordner
        gelöscht.

        Returns:
            Die Statistik dieser Quelle.
        """
        dirs = self.scan()
        items = [it for _, media, _ in dirs for it in media if isinstance(it, MediaItem)]
        if items:
            log.debug("%s: %d Dateien gefunden, lese Metadaten …", self.name, len(items))
            try:
                meta = run_exiftool(self.cfg.exiftool, [it.path for it in items])
            except ExiftoolError as e:
                log.error("%s: exiftool fehlgeschlagen, Quelle wird übersprungen: %s", self.name, e)
                self.stats.errors += 1
                return self.stats
            for dirpath, media, sidecars in dirs:
                self.process_dir(dirpath, media, sidecars, meta)
        if self.cfg.remove_empty_dirs:
            self.remove_empty_dirs()
        return self.stats

    def is_excluded(self, dirname):
        """Prüft, ob ein Ordner übersprungen wird.

        Args:
            dirname: Name eines einzelnen Ordners (kein Pfad).

        Returns:
            True für versteckte Ordner (".", "@"), Synology-Systemordner und
            Ordner aus exclude_dirs.
        """
        d = dirname.lower()
        return (d.startswith(".") or d.startswith("@") or d in ALWAYS_EXCLUDED_DIRS
                or d in self.cfg.exclude_dirs)

    def scan(self):
        """Liefert pro Ordner (Pfad, Mediendateien, Begleitdateien).

        Noch zu junge Dateien stehen als _Young in der Liste, damit ihre Begleitdateien
        nicht versehentlich einer anderen Datei mit gleichem Stamm zugeordnet werden.

        Returns:
            Liste von (Ordnerpfad, Liste aus MediaItem/_Young, Liste der
            Begleitdatei-Pfade); nur Ordner mit mindestens einer Mediendatei.
        """
        result = []
        now = time.time()
        min_age = self.cfg.min_age_minutes * 60

        def onerror(err):
            """Zählt einen nicht lesbaren Ordner als Fehler (für os.walk)."""
            log.error("%s: Ordner nicht lesbar: %s", self.name, err)
            self.stats.errors += 1

        for dirpath, dirnames, filenames in os.walk(self.root, onerror=onerror):
            dirnames[:] = sorted(d for d in dirnames if not self.is_excluded(d))
            media = []  # type: List
            sidecars = []  # type: List[str]
            for fn in sorted(filenames):
                if fn.startswith(".") or fn.endswith(TMP_SUFFIX):
                    continue
                ext = os.path.splitext(fn)[1][1:].lower()
                path = os.path.join(dirpath, fn)
                if ext in self.cfg.media_ext:
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    # ctime ändert sich bei jedem Schreibzugriff – mtime setzen
                    # Sync-Apps oft auf das Aufnahmedatum zurück
                    if now - max(st.st_mtime, st.st_ctime) < min_age:
                        self.stats.young += 1
                        media.append(_Young(fn))
                        log.debug("%s: zu jung, später: %s", self.name, self.rel(path))
                    else:
                        media.append(MediaItem(path))
                elif ext in self.cfg.sidecar_ext:
                    sidecars.append(path)
            if media:
                result.append((dirpath, media, sidecars))
        return result

    def process_dir(self, dirpath, media, sidecar_paths, meta):
        """Ermittelt Datum und Ziel aller Dateien eines Ordners und sortiert sie ein.

        Dateien ohne exiftool-Eintrag bleiben liegen und zählen als Fehler.
        Dateien ohne Datum gehen nach nodate_folder.

        Args:
            dirpath: Der Ordner (nur zur Information).
            media: MediaItem- und _Young-Einträge aus scan().
            sidecar_paths: Begleitdateien in diesem Ordner.
            meta: Ergebnis von run_exiftool().
        """
        items = [m for m in media if isinstance(m, MediaItem)]
        self.attach_sidecars(media, sidecar_paths)

        ready = []
        for it in items:
            entry = meta.get(it.path)
            if entry is None:
                log.error("%s: keine Metadaten von exiftool erhalten, Datei bleibt liegen: %s",
                          self.name, self.rel(it.path))
                self.stats.errors += 1
                continue
            if entry.get("Error"):
                log.debug("%s: exiftool meldet für %s: %s", self.name, self.rel(it.path), entry["Error"])
            it.date, it.how = date_from_metadata(entry)
            if it.date is None:
                it.date = date_from_filename(it.name)
                it.how = "Dateiname" if it.date else ""
            it.dest_dir = (target_path(self.cfg, it.date) if it.date
                           else os.path.join(self.root, self.cfg.nodate_folder))
            ready.append(it)

        # Dateien mit gleichem Stamm und gleichem Ziel (z.B. Live Photo HEIC + MOV)
        # bekommen bei Namenskonflikten dieselbe Nummer, damit sie zusammen bleiben
        groups = {}  # type: Dict[Tuple[str, str], List[MediaItem]]
        for it in ready:
            groups.setdefault((it.stem.lower(), it.dest_dir), []).append(it)
        for (_, dest_dir), group in sorted(groups.items()):
            self.place_group(group, dest_dir)

    def attach_sidecars(self, media, sidecar_paths):
        """Ordnet Begleitdateien ihrer Mediendatei zu.

        Zuerst wird der volle Dateiname gesucht (IMG_1.HEIC.xmp -> IMG_1.HEIC),
        dann der Stamm (IMG_1.AAE -> IMG_1.HEIC); dabei hat ein Bild Vorrang vor
        einem Video. Begleitdateien ohne passende Mediendatei bleiben liegen.

        Args:
            media: MediaItem- und _Young-Einträge des Ordners; deren
                sidecars-Listen werden ergänzt.
            sidecar_paths: Begleitdateien im selben Ordner.
        """
        by_name = {m.name.lower(): m for m in media}
        by_stem = {}  # type: Dict[str, List]
        for m in media:
            by_stem.setdefault(m.stem.lower(), []).append(m)
        for sc_path in sidecar_paths:
            sc_name = os.path.basename(sc_path)
            base, tail = os.path.splitext(sc_name)
            owner = by_name.get(base.lower())
            if owner is not None:
                owner.sidecars.append(Sidecar(sc_path, tail, True))
                continue
            candidates = by_stem.get(base.lower())
            if candidates:
                # Bild vor Video (iOS: IMG_1.AAE gehört zu IMG_1.HEIC, nicht zu IMG_1.MOV)
                owner = sorted(candidates, key=lambda m: (m.ext[1:].lower() not in self.cfg.image_ext,
                                                          m.name))[0]
                owner.sidecars.append(Sidecar(sc_path, tail, False))

    def plan_group(self, group, dest_dir, suffix):
        """Plan für eine Nummer – oder None, wenn ein anderer Inhalt den Namen belegt.

        Ein Name, unter dem schon eine inhaltsgleiche Datei liegt, ist kein
        Konflikt: Die Datei wird dann als Duplikat behandelt.

        Args:
            group: Mediendateien mit gleichem Stamm und gleichem Ziel.
            dest_dir: Zielordner.
            suffix: Anhang an den Stamm, "" oder z.B. "_1".

        Returns:
            Liste von (MediaItem, neuer Name, vorhandene gleiche Datei oder None,
            [(Sidecar, neuer Name, vorhandene gleiche Datei oder None), …]),
            oder None, wenn ein Name mit anderem Inhalt belegt ist.
        """
        plan = []
        for it in group:
            new_stem = it.stem + suffix
            new_name = new_stem + it.ext
            existing = self.index.get(dest_dir, new_name)
            if existing is not None and not self.compare.same(it.path, existing):
                return None
            sc_plan = []
            for sc in it.sidecars:
                sc_new = (new_name if sc.full else new_stem) + sc.tail
                sc_existing = self.index.get(dest_dir, sc_new)
                if sc_existing is not None and not self.compare.same(sc.path, sc_existing):
                    return None
                sc_plan.append((sc, sc_new, sc_existing))
            plan.append((it, new_name, existing, sc_plan))
        return plan

    def place_group(self, group, dest_dir):
        """Sortiert eine Gruppe samt Begleitdateien unter einem gemeinsamen freien Namen ein.

        Probiert die Nummern "", "_1", "_2", … bis zu MAX_RENAME. Fehler bei
        einer Datei werden geloggt und gezählt; die Datei bleibt dann liegen.

        Args:
            group: Mediendateien mit gleichem Stamm und gleichem Ziel.
            dest_dir: Zielordner.
        """
        plan = None
        for i in range(MAX_RENAME):
            plan = self.plan_group(group, dest_dir, "" if i == 0 else "_%d" % i)
            if plan is not None:
                break
        if plan is None:
            for it in group:
                log.error("%s: kein freier Dateiname in %s für %s", self.name, dest_dir, it.name)
                self.stats.errors += 1
            return

        for it, new_name, existing, sc_plan in plan:
            try:
                if existing is not None:
                    dup = self.to_duplicate(it.path)
                    self.stats.duplicates += 1
                    self.info("DUPLIKAT    %s -> %s (identisch mit %s)",
                              self.rel(it.path), self.rel(dup), self.rel(existing))
                else:
                    self.transfer(it.path, dest_dir, new_name)
                    renamed = new_name != it.name
                    if it.date is None:
                        self.stats.nodate += 1
                        self.info("OHNE DATUM  %s -> %s", self.rel(it.path),
                                  self.rel(os.path.join(dest_dir, new_name)))
                    else:
                        self.stats.moved += 1
                        self.stats.renamed += renamed
                        self.info("%s %s -> %s (%s: %s)", "UMBENANNT  " if renamed else "EINSORTIERT",
                                  self.rel(it.path), self.rel(os.path.join(dest_dir, new_name)),
                                  it.how, it.date.strftime("%Y-%m-%d %H:%M:%S"))
            except OSError as e:
                log.error("%s: Fehler bei %s, Datei bleibt liegen: %s", self.name, self.rel(it.path), e)
                self.stats.errors += 1
                continue  # Begleitdateien bleiben dann auch liegen

            for sc, sc_new, sc_existing in sc_plan:
                try:
                    if sc_existing is not None:
                        dup = self.to_duplicate(sc.path)
                        self.info("  Begleitdatei %s -> %s (Duplikat)", self.rel(sc.path), self.rel(dup))
                    else:
                        self.transfer(sc.path, dest_dir, sc_new)
                        self.info("  Begleitdatei %s -> %s", self.rel(sc.path),
                                  self.rel(os.path.join(dest_dir, sc_new)))
                    self.stats.sidecars += 1
                except OSError as e:
                    log.error("%s: Fehler bei Begleitdatei %s: %s", self.name, self.rel(sc.path), e)
                    self.stats.errors += 1

    def transfer(self, src, dest_dir, name):
        """Kopieren, prüfen, Original löschen.

        Im Testmodus wird nur der Name im Index vorgemerkt.

        Args:
            src: Quelldatei.
            dest_dir: Zielordner; wird bei Bedarf angelegt.
            name: Dateiname im Zielordner.

        Returns:
            Pfad der Zieldatei.

        Raises:
            OSError: Kopieren, Prüfen oder Löschen fehlgeschlagen.
        """
        dst = os.path.join(dest_dir, name)
        if self.dry:
            self.index.add(dest_dir, name, src)
            return dst
        os.makedirs(dest_dir, exist_ok=True)
        copy_verified(src, dst)
        self.index.add(dest_dir, name, dst)
        remove_source(src)
        return dst

    def to_duplicate(self, src):
        """Verschiebt eine Datei in den duplicate-Ordner der Quelle.

        Ist der Name dort schon belegt, wird "_1", "_2", … angehängt.

        Args:
            src: Die zu verschiebende Datei.

        Returns:
            Pfad im duplicate-Ordner.

        Raises:
            OSError: Verschieben fehlgeschlagen oder kein freier Name.
        """
        dup_dir = os.path.join(self.root, self.cfg.duplicate_folder)
        stem, ext = os.path.splitext(os.path.basename(src))
        for i in range(MAX_RENAME):
            name = stem + ("_%d" % i if i else "") + ext
            if self.index.get(dup_dir, name) is None:
                return self.transfer(src, dup_dir, name)
        raise IOError("kein freier Dateiname in %s" % dup_dir)

    def remove_empty_dirs(self):
        """Löscht leere Unterordner des Upload-Ordners.

        Ein Ordner, der nur noch @eaDir enthält, gilt als leer. Der Upload-Ordner
        selbst und ausgeschlossene Ordner bleiben stehen. Fehler werden nur als
        Warnung geloggt.
        """
        for dirpath, dirnames, filenames in os.walk(self.root, topdown=False):
            if os.path.samefile(dirpath, self.root):
                continue
            parts = os.path.relpath(dirpath, self.root).split(os.sep)
            if any(self.is_excluded(p) for p in parts):
                continue
            try:
                entries = os.listdir(dirpath)
            except OSError:
                continue
            if [e for e in entries if e != "@eaDir"]:
                continue
            if self.dry:
                self.info("LEERER ORDNER %s würde gelöscht", self.rel(dirpath))
                continue
            try:
                if entries:
                    shutil.rmtree(os.path.join(dirpath, "@eaDir"))
                os.rmdir(dirpath)
                log.info("LEERER ORDNER %s gelöscht", self.rel(dirpath))
            except OSError as e:
                log.warning("%s: Ordner %s nicht gelöscht: %s", self.name, self.rel(dirpath), e)


class _Young:
    """Platzhalter für eine noch zu junge Datei (nur für die Zuordnung von Begleitdateien)."""

    def __init__(self, name):
        """
        Args:
            name: Dateiname der zu jungen Datei.
        """
        self.name = name
        self.stem, self.ext = os.path.splitext(name)
        self.sidecars = []  # type: List[Sidecar]


# --------------------------------------------------------------------------- #
# Logging, Sperre, Hauptprogramm
# --------------------------------------------------------------------------- #

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


def setup_console_logging(verbose):
    """Leitet alle Logmeldungen nach stdout.

    Args:
        verbose: True für Debug-Meldungen, sonst nur Info und höher.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def source_log_handler(cfg, root):
    """Monatliche Logdatei im logs-Ordner des Nutzers. Wird erst beim ersten Eintrag angelegt.

    Args:
        cfg: Konfiguration (log_folder).
        root: Upload-Ordner der Quelle.

    Returns:
        Ein logging.FileHandler für logs/synphotomove_JJJJ-MM.log.

    Raises:
        OSError: Der Logordner kann nicht angelegt werden.
    """
    log_dir = os.path.join(root, cfg.log_folder)
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "%s%s.log" % (LOG_PREFIX, datetime.date.today().strftime("%Y-%m")))
    handler = logging.FileHandler(path, encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, "%Y-%m-%d %H:%M:%S"))
    handler.setLevel(logging.INFO)
    return handler


def cleanup_old_logs(cfg, root):
    """Löscht Logdateien, die älter als log_keep_months sind.

    Fehler werden ignoriert.

    Args:
        cfg: Konfiguration (log_folder, log_keep_months).
        root: Upload-Ordner der Quelle.
    """
    log_dir = os.path.join(root, cfg.log_folder)
    today = datetime.date.today()
    oldest = today.year * 12 + today.month - 1 - cfg.log_keep_months
    pattern = re.compile(r"^%s(\d{4})-(\d{2})\.log$" % LOG_PREFIX)
    try:
        names = os.listdir(log_dir)
    except OSError:
        return
    for n in names:
        m = pattern.match(n)
        if m and int(m.group(1)) * 12 + int(m.group(2)) - 1 < oldest:
            try:
                os.remove(os.path.join(log_dir, n))
            except OSError:
                pass


def acquire_lock(path):
    """Setzt die Sperre gegen parallele Läufe.

    Die Sperre hält, solange die zurückgegebene Datei offen ist, und wird vom
    Betriebssystem auch bei einem Absturz freigegeben.

    Args:
        path: Pfad der Sperrdatei.

    Returns:
        Die geöffnete Sperrdatei, oder None, wenn ein anderer Lauf sie hält.

    Raises:
        OSError: Die Sperrdatei kann nicht geöffnet werden.
    """
    fh = open(path, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def parse_args(argv):
    """Liest die Kommandozeilenoptionen.

    Args:
        argv: Argumente ohne Programmnamen; None für sys.argv[1:].

    Returns:
        argparse.Namespace mit den Optionen.
    """
    p = argparse.ArgumentParser(
        description="Sortiert Fotos/Videos aus Upload-Ordnern nach Aufnahmedatum in die Fotoablage.")
    p.add_argument("-c", "--config", help="Konfigurationsdatei (INI)")
    p.add_argument("--target", help="Zielordner (überschreibt die Konfiguration)")
    p.add_argument("--source", action="append", help="Quellordner (mehrfach möglich, ergänzt [sources])")
    p.add_argument("--only", action="append", metavar="NAME",
                   help="Nur diese Quelle(n) aus [sources] verarbeiten")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="Testmodus: nur anzeigen, nichts verändern")
    p.add_argument("--min-age", type=float, metavar="MINUTEN",
                   help="Dateien jünger als MINUTEN überspringen")
    p.add_argument("--remove-empty-dirs", action="store_true",
                   help="Leere Unterordner in den Upload-Ordnern löschen")
    p.add_argument("-v", "--verbose", action="store_true", help="Ausführliche Ausgabe")
    p.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    return p.parse_args(argv)


def main(argv=None):
    """Hauptprogramm: verarbeitet alle Quellen nacheinander.

    Args:
        argv: Argumente ohne Programmnamen; None für sys.argv[1:].

    Returns:
        Exit-Code: 0 ok (auch wenn ein anderer Lauf noch aktiv ist),
        1 Fehler bei einzelnen Dateien oder Quellen, 2 Konfigurationsfehler.
    """
    args = parse_args(argv)
    setup_console_logging(args.verbose)
    try:
        cfg = load_config(args)
    except ConfigError as e:
        log.error("%s", e)
        return 2

    if args.only:
        unknown = set(args.only) - set(cfg.sources)
        if unknown:
            log.error("Unbekannte Quelle(n): %s", ", ".join(sorted(unknown)))
            return 2
        cfg.sources = {k: v for k, v in cfg.sources.items() if k in args.only}

    lock = acquire_lock(cfg.lock_file)
    if lock is None:
        log.info("Ein anderer Lauf ist noch aktiv – beende mich.")
        return 0

    try:
        if cfg.dry_run:
            log.info("TESTMODUS – es werden keine Dateien verändert.")
        total = Stats()
        for name, root in cfg.sources.items():
            if not os.path.isdir(root):
                log.error("%s: Quellordner existiert nicht: %s", name, root)
                total.errors += 1
                continue
            handler = None
            if not cfg.dry_run:
                try:
                    handler = source_log_handler(cfg, root)
                    log.addHandler(handler)
                except OSError as e:
                    log.warning("%s: Logordner nicht beschreibbar: %s", name, e)
            try:
                stats = SourceProcessor(cfg, name, root).run()
                if stats.anything():
                    log.info("%s: %s", name, stats)
                else:
                    log.debug("%s: %s", name, stats)
            finally:
                if handler is not None:
                    log.removeHandler(handler)
                    handler.close()
            if not cfg.dry_run:
                cleanup_old_logs(cfg, root)
            total.add(stats)
        log.info("Fertig: %s", total)
        return 1 if total.errors else 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
