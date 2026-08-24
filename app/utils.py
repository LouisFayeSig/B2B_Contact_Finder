from __future__ import annotations

import json
import time
from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.models import CompanyResult

NOT_FOUND_LABEL = "Non trouvé"

_MISSING_TOKENS = {
    "",
    "none",
    "null",
    "unknown",
    "n/a",
    "na",
    "not found",
    "introuvable",
    "aucun",
    "aucune",
    "non disponible",
    "non renseigne",
    "non renseigné",
    "non trouve",
    "non trouvé",
}


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "oui", "on"}:
        return True
    if text in {"0", "false", "no", "n", "non", "off"}:
        return False
    return default


def parse_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return int(text)


def parse_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return float(text)


def clean_cell_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = " ".join(value.strip().split())
        return cleaned or None
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return format(normalized, "f").rstrip("0").rstrip(".") or "0"
    if isinstance(value, Path):
        return str(value)
    return str(value).strip() or None


def normalize_missing_value(value: Any) -> str:
    cleaned = clean_cell_value(value)
    if cleaned is None:
        return NOT_FOUND_LABEL
    normalized = cleaned.lower()
    if normalized in _MISSING_TOKENS:
        return NOT_FOUND_LABEL
    return cleaned


def sanitize_result(value: CompanyResult | dict[str, Any] | None) -> CompanyResult:
    payload: dict[str, Any]
    if value is None:
        payload = {}
    elif isinstance(value, CompanyResult):
        payload = value.model_dump()
    else:
        payload = dict(value)

    return CompanyResult(
        email=normalize_missing_value(payload.get("email")),
        phone=normalize_missing_value(payload.get("phone")),
        website=normalize_missing_value(payload.get("website")),
    )


def chunked(items: Sequence[Any], size: int) -> Iterator[list[Any]]:
    if size <= 0:
        raise ValueError("size must be greater than 0")
    for index in range(0, len(items), size):
        yield list(items[index : index + size])


def safe_sleep(seconds: float, *, logger: Any | None = None, reason: str | None = None) -> None:
    if seconds <= 0:
        return
    if logger is not None and reason:
        logger.debug("Pause de %.2f seconde(s) (%s).", seconds, reason)
    time.sleep(seconds)


def maybe_limit_rows(row_indexes: Sequence[int], max_rows: int | None) -> list[int]:
    if max_rows is None or max_rows <= 0:
        return list(row_indexes)
    return list(row_indexes[:max_rows])


def extract_first_json_object(text: str) -> str | None:
    """Extrait le premier objet JSON complet dans un texte libre."""

    if not text:
        return None

    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    return None


def try_parse_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        return parsed

    candidate = extract_first_json_object(text)
    if not candidate:
        return None

    try:
        reparsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None

    if isinstance(reparsed, dict):
        return reparsed
    return None
