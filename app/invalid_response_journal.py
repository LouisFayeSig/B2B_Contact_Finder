from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.models import CompanyRow


class InvalidResponseJournal:
    """Append-only diagnostic journal for unusable Foundry responses."""

    def __init__(self, path: Path, logger: Any) -> None:
        self.path = path
        self.logger = logger
        self._lock = threading.Lock()

    def append(
        self,
        company: CompanyRow,
        response_payload: Any,
        response_text: str,
        error: BaseException,
        *,
        attempt: int,
    ) -> None:
        record = {
            "recorded_at_utc": datetime.now(UTC).isoformat(),
            "attempt": attempt,
            "row_index": company.row_index,
            "siret": company.siret,
            "company_name": company.company_name,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "response_text": response_text,
            "response_payload": response_payload,
        }
        try:
            serialized = json.dumps(record, ensure_ascii=False, default=str)
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(serialized)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except (OSError, TypeError, ValueError) as journal_error:
            self.logger.error(
                "Impossible de sauvegarder la reponse Foundry invalide dans %s : %s",
                self.path,
                journal_error,
            )
