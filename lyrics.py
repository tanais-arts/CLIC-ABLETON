"""Charge les paroles d'un morceau (fichier `Lyrics/<nom de scène>.csv`,
export Ableton Live "Convertir en texte") pour les associer aux mesures
affichées par CLIC (voir beat_display.py).

Le fichier a 6 colonnes ; seule la 6e ("Discours") est utilisée, les 5
premières (piste, clip, temps de début/fin, durée) sont ignorées. Chaque
ligne du fichier (après l'en-tête) représente un bloc fixe de 8 mesures,
dans l'ordre : la 1re ligne couvre les mesures 1 à 8, la 2e les mesures 9 à
16, etc. — indépendant des colonnes de temps du CSV, qui ne reflètent pas ce
découpage. Certaines lignes sont vides (pas de chant sur ce passage).

Fichier absent, illisible ou vide -> None : comportement "normal, pas de
paroles" attendu par beat_display.py, jamais une exception.
"""
from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

BARS_PER_LINE = 8
TEXT_COLUMN_INDEX = 5  # colonne "Discours" (6e, 0-indexée)


def _canonical_scene_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name.replace("’", "'"))
    normalized = normalized.encode("ascii", "ignore").decode().lower()
    normalized = re.sub(r"\b[ldjtmns]'", "", normalized)
    return "".join(character for character in normalized if character.isalnum())


def _one_substitution_apart(left: str, right: str) -> bool:
    return len(left) == len(right) and sum(a != b for a, b in zip(left, right)) == 1


def _lyrics_path(scene_name: str, base_dir: Path) -> Path | None:
    folder = base_dir / "Lyrics"
    exact = folder / f"{scene_name}.csv"
    if exact.is_file():
        return exact

    wanted = _canonical_scene_name(scene_name)
    candidates = list(folder.glob("*.csv"))
    normalized_matches = [path for path in candidates if _canonical_scene_name(path.stem) == wanted]
    if len(normalized_matches) == 1:
        return normalized_matches[0]
    if normalized_matches:
        return None

    near_matches = [
        path for path in candidates
        if _one_substitution_apart(_canonical_scene_name(path.stem), wanted)
    ]
    return near_matches[0] if len(near_matches) == 1 else None


@dataclass(frozen=True)
class LyricsSheet:
    lines: list[str]  # texte de chaque ligne du CSV, dans l'ordre (peut être vide)

    def line_index_for_bar(self, mes: int) -> int | None:
        """Index (0-based) de la ligne de paroles couvrant la mesure `mes`,
        ou None si `mes` < 1 ou au-delà de la dernière ligne connue."""
        if mes < 1:
            return None
        index = (mes - 1) // BARS_PER_LINE
        if index >= len(self.lines):
            return None
        return index

    def text_for_bar(self, mes: int) -> str:
        """Texte (peut être vide) de la ligne de paroles couvrant la mesure
        `mes`, chaîne vide si `mes` est hors feuille."""
        index = self.line_index_for_bar(mes)
        if index is None:
            return ""
        return self.lines[index]


def load_lyrics(scene_name: str, base_dir: Path, log=print) -> LyricsSheet | None:
    """Charge le CSV de paroles correspondant au nom de scène, sinon None."""
    if not scene_name:
        return None
    path = _lyrics_path(scene_name, base_dir)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    except OSError as exc:
        log(f"Paroles {path.name} ignorées : {exc}")
        return None
    if not rows:
        return None
    lines = [
        values[TEXT_COLUMN_INDEX].strip() if len(values) > TEXT_COLUMN_INDEX else ""
        for values in rows[1:]  # 1re ligne = en-tête, ignorée
    ]
    return LyricsSheet(lines=lines)


def save_lyrics_line(scene_name: str, base_dir: Path, line_index: int, text: str, log=print) -> bool:
    """Réécrit la colonne "Discours" de la ligne `line_index` (0-based, après
    l'en-tête) dans le CSV, sans toucher aux autres colonnes/lignes (édition
    en direct depuis beat_display.py). False si le fichier ou la ligne
    n'existe pas (jamais d'exception)."""
    path = _lyrics_path(scene_name, base_dir)
    if path is None:
        return False
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    except OSError as exc:
        log(f"Paroles {path.name} : lecture impossible avant écriture ({exc})")
        return False
    row_pos = line_index + 1  # +1 pour l'en-tête
    if row_pos >= len(rows):
        return False
    row = rows[row_pos]
    while len(row) <= TEXT_COLUMN_INDEX:
        row.append("")
    row[TEXT_COLUMN_INDEX] = text
    try:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            # lineterminator="\n" : le CSV exporté par Live n'a pas de \r,
            # sinon chaque ligne (pas juste celle éditée) change dans le diff.
            csv.writer(handle, lineterminator="\n").writerows(rows)
    except OSError as exc:
        log(f"Paroles {path.name} : écriture impossible ({exc})")
        return False
    return True
