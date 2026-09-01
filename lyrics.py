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
from dataclasses import dataclass
from pathlib import Path

BARS_PER_LINE = 8
TEXT_COLUMN_INDEX = 5  # colonne "Discours" (6e, 0-indexée)


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
    """Charge `<base_dir>/Lyrics/<scene_name>.csv` si le fichier existe, sinon None."""
    if not scene_name:
        return None
    path = base_dir / "Lyrics" / f"{scene_name}.csv"
    if not path.is_file():
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
