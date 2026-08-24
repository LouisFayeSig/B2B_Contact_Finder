from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.cell import Cell
from openpyxl.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from app.models import CompanyResult, CompanyRow
from app.utils import clean_cell_value, maybe_limit_rows


@dataclass(frozen=True, slots=True)
class ExcelColumns:
    siret: int = 3
    company_name: int = 4
    address: int = 7
    postal_code: int = 8
    city: int = 9
    email: int = 16
    phone: int = 17
    website: int = 18


class ExcelService:
    def __init__(self, file_path: Path, sheet_name: str | None, logger: Any) -> None:
        self.file_path = file_path
        self.sheet_name = sheet_name
        self.logger = logger
        self.columns = ExcelColumns()
        self._workbook: Workbook | None = None
        self._sheet: Worksheet | None = None

    @property
    def workbook(self) -> Workbook:
        if self._workbook is None:
            raise RuntimeError("Workbook non ouvert.")
        return self._workbook

    @property
    def sheet(self) -> Worksheet:
        if self._sheet is None:
            raise RuntimeError("Worksheet non selectionnee.")
        return self._sheet

    def open(self) -> None:
        self.logger.info("Ouverture du fichier Excel : %s", self.file_path)
        self._workbook = load_workbook(filename=self.file_path)

        if self.sheet_name:
            if self.sheet_name not in self.workbook.sheetnames:
                raise ValueError(f"Feuille introuvable : {self.sheet_name}")
            self._sheet = self.workbook[self.sheet_name]
        else:
            self._sheet = self.workbook.active

        self.logger.info("Feuille selectionnee : %s", self.sheet.title)

    def create_backup(self) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self.file_path.with_name(
            f"{self.file_path.stem}.backup.{timestamp}{self.file_path.suffix}"
        )
        shutil.copy2(self.file_path, backup_path)
        self.logger.info("Backup cree : %s", backup_path)
        return backup_path

    def save(self) -> None:
        self.workbook.save(self.file_path)

    def get_last_useful_row(self) -> int:
        for row_index in range(self.sheet.max_row, 1, -1):
            if any(
                self._read_text(row_index, column_index) is not None
                for column_index in (
                    self.columns.siret,
                    self.columns.company_name,
                    self.columns.address,
                    self.columns.postal_code,
                    self.columns.city,
                )
            ):
                return row_index
        return 1

    def get_row_indexes(self, start_row: int, max_rows: int | None) -> list[int]:
        last_row = self.get_last_useful_row()
        if last_row < start_row:
            return []
        row_indexes = list(range(start_row, last_row + 1))
        return maybe_limit_rows(row_indexes, max_rows)

    def read_company_row(self, row_index: int) -> CompanyRow:
        return CompanyRow(
            row_index=row_index,
            siret=self._read_text(row_index, self.columns.siret) or "",
            company_name=self._read_text(row_index, self.columns.company_name) or "",
            address=self._read_text(row_index, self.columns.address),
            postal_code=self._read_text(row_index, self.columns.postal_code),
            city=self._read_text(row_index, self.columns.city),
        )

    def are_result_cells_filled(self, row_index: int) -> bool:
        values = (
            self._read_text(row_index, self.columns.email),
            self._read_text(row_index, self.columns.phone),
            self._read_text(row_index, self.columns.website),
        )
        return all(value is not None and value != "" for value in values)

    def write_result(self, row_index: int, result: CompanyResult) -> None:
        self.sheet.cell(row=row_index, column=self.columns.email, value=result.email)
        self.sheet.cell(row=row_index, column=self.columns.phone, value=result.phone)
        self.sheet.cell(row=row_index, column=self.columns.website, value=result.website)

    def _read_text(self, row_index: int, column_index: int) -> str | None:
        cell = self.sheet.cell(row=row_index, column=column_index)
        return self._cell_to_text(cell)

    def _cell_to_text(self, cell: Cell) -> str | None:
        value = cell.value
        if value is None:
            return None
        if isinstance(value, str):
            return clean_cell_value(value)
        if isinstance(value, bool):
            return "True" if value else "False"
        if isinstance(value, int):
            return self._format_numeric_cell(cell, value)
        if isinstance(value, float):
            integer_value = int(value) if value.is_integer() else value
            return self._format_numeric_cell(cell, integer_value)
        return clean_cell_value(value)

    def _format_numeric_cell(self, cell: Cell, value: int | float) -> str:
        if isinstance(value, float) and not value.is_integer():
            return str(value)

        integer_value = int(value)
        number_format = str(cell.number_format or "")

        # Preserve les codes postaux stockes comme nombres avec un format du type 00000.
        normalized_format = number_format.replace("\\", "").replace('"', "").strip()
        if "0" in normalized_format and all(char in {"0", "#"} for char in normalized_format):
            width = normalized_format.count("0")
            return str(integer_value).zfill(width)

        return str(integer_value)
