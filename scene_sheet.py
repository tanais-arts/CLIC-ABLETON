"""Lit un fichier XLSX de "feuille de scène" (même nom que la scène Live en
cours) pour adapter, mesure par mesure, le nombre de temps affiché, un
surlignage visuel et un texte de section (INTRO/COUPLET/REFRAIN...). Voir
beat_display.py (`_apply_scene_sheet_row`) et README.md.

Colonnes attendues dans la première ligne (n'importe quel ordre, insensible
à la casse) : MES (numéro de mesure depuis le début du morceau, entier),
COUNT (temps par mesure pour cette mesure, entier), HIGHLIGHT (1 = mesure à
surligner, 0/vide = normal), LABEL (texte libre).

Fichier absent, illisible, mal formé, ou openpyxl non installé -> None :
c'est le comportement "normal, pas de feuille" attendu par beat_display.py,
jamais une exception.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REQUIRED_COLUMNS = ("MES", "COUNT", "HIGHLIGHT", "LABEL")


@dataclass(frozen=True)
class SceneSheetRow:
    count: int | None
    highlight: bool
    label: str


class SceneSheet:
    def __init__(self, rows: dict[int, SceneSheetRow]):
        self._rows = rows

    def get(self, mes: int) -> SceneSheetRow | None:
        return self._rows.get(mes)

    def label_at_or_before(self, mes: int) -> str:
        """Dernier LABEL non vide à la mesure `mes` ou avant : sert à
        retrouver le bon label "collant" en cas de départ (GO TO) qui ne
        tombe pas exactement sur une mesure étiquetée."""
        for candidate in range(mes, 0, -1):
            row = self._rows.get(candidate)
            if row is not None and row.label:
                return row.label
        return ""


def load_scene_sheet(scene_name: str, base_dir: Path, log=print) -> SceneSheet | None:
    """Charge `<base_dir>/<scene_name>.xlsx` si le fichier existe, sinon None."""
    if not scene_name:
        return None
    path = base_dir / f"{scene_name}.xlsx"
    if not path.is_file():
        return None
    try:
        import openpyxl
    except ImportError:
        log(f"Feuille de scène {path.name} ignorée : le module openpyxl n'est pas installé.")
        return None
    try:
        workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
        sheet = workbook.worksheets[0]
        rows_iter = sheet.iter_rows(values_only=True)
        header = next(rows_iter)
        columns = {
            str(name).strip().upper(): index
            for index, name in enumerate(header) if name is not None
        }
        missing = [col for col in REQUIRED_COLUMNS if col not in columns]
        if missing:
            log(f"Feuille de scène {path.name} ignorée : colonnes manquantes {missing}.")
            return None
        rows: dict[int, SceneSheetRow] = {}
        for values in rows_iter:
            mes_value = values[columns["MES"]]
            if mes_value is None:
                continue
            try:
                mes = int(mes_value)
            except (TypeError, ValueError):
                continue
            count_value = values[columns["COUNT"]]
            try:
                count = int(count_value) if count_value is not None else None
            except (TypeError, ValueError):
                count = None
            highlight = values[columns["HIGHLIGHT"]] == 1
            label_value = values[columns["LABEL"]]
            label = str(label_value).strip() if label_value else ""
            rows[mes] = SceneSheetRow(count=count, highlight=highlight, label=label)
        return SceneSheet(rows)
    except Exception as exc:  # fichier corrompu/format inattendu : ne jamais bloquer l'appli
        log(f"Feuille de scène {path.name} ignorée : {exc}")
        return None
