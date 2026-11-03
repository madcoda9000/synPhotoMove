#!/usr/bin/env python3
"""Ersatz für exiftool in den Tests.

Liest die Dateiliste aus der -@-Argumentdatei und liefert für jede Datei den Inhalt
aus der JSON-Datei in $FAKE_EXIF (Schlüssel = Dateiname). Unbekannte Dateien
bekommen nur SourceFile, der Wert "OMIT" lässt die Datei in der Ausgabe weg.
"""
import json
import os
import sys

args = sys.argv[1:]
argfile = args[args.index("-@") + 1]
with open(os.environ["FAKE_EXIF"], encoding="utf-8") as fh:
    data = json.load(fh)
with open(argfile, encoding="utf-8") as fh:
    paths = [line.rstrip("\n") for line in fh if line.strip()]
out = []
for p in paths:
    if data.get(os.path.basename(p)) == "OMIT":
        continue
    entry = {"SourceFile": p}
    entry.update(data.get(os.path.basename(p), {}))
    out.append(entry)
print(json.dumps(out))
