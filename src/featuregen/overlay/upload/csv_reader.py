from __future__ import annotations

import csv
import io

from featuregen.overlay.upload._headers import build_row, field_map
from featuregen.overlay.upload.canonical import CanonicalRow


def read_csv_rows(text: str, *, source: str) -> list[CanonicalRow]:
    reader = csv.DictReader(io.StringIO(text))
    fmap = field_map(list(reader.fieldnames or []))
    return [build_row(fmap, raw, source) for raw in reader]
