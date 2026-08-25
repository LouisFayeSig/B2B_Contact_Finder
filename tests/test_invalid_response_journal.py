from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from app.invalid_response_journal import InvalidResponseJournal
from app.models import CompanyRow


def test_invalid_response_is_saved_as_jsonl() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        path = Path(temporary_directory) / "invalid.jsonl"
        journal = InvalidResponseJournal(path, logging.getLogger("tests.invalid_response"))
        company = CompanyRow(row_index=23, siret="12345678901234", company_name="Entreprise Test")

        journal.append(
            company,
            {"id": "resp_bad", "status": "completed"},
            "{json incomplet",
            ValueError("parsing impossible"),
            attempt=1,
        )

        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["row_index"] == 23
        assert record["response_text"] == "{json incomplet"
        assert record["response_payload"]["id"] == "resp_bad"
        assert record["attempt"] == 1
