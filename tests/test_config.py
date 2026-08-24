from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.config import AppConfig, _normalize_foundry_endpoint


def test_foundry_endpoint_appends_openai_v1_route() -> None:
    endpoint = _normalize_foundry_endpoint("https://resource.services.ai.azure.com/api/projects/project")

    assert endpoint == ("https://resource.services.ai.azure.com/api/projects/project/openai/v1/")


def test_foundry_endpoint_preserves_complete_route() -> None:
    endpoint = _normalize_foundry_endpoint("https://resource.openai.azure.com/openai/v1/")

    assert endpoint == "https://resource.openai.azure.com/openai/v1/"


def test_zero_retry_wait_is_not_replaced_by_default() -> None:
    environment = {
        "AZURE_FOUNDRY_ENDPOINT": "https://resource.openai.azure.com/openai/v1/",
        "AZURE_FOUNDRY_CA_BUNDLE": "",
        "INPUT_EXCEL_PATH": "data/test.xlsx",
        "MAX_WORKERS": "4",
        "RETRY_WAIT_SECONDS": "0",
    }

    with patch.dict(os.environ, environment, clear=True):
        config = AppConfig.from_env()

    assert config.retry_wait_seconds == 0


def test_invalid_zero_worker_count_is_rejected() -> None:
    environment = {
        "AZURE_FOUNDRY_ENDPOINT": "https://resource.openai.azure.com/openai/v1/",
        "AZURE_FOUNDRY_CA_BUNDLE": "",
        "INPUT_EXCEL_PATH": "data/test.xlsx",
        "MAX_WORKERS": "0",
    }

    with patch.dict(os.environ, environment, clear=True), pytest.raises(ValueError, match="MAX_WORKERS"):
        AppConfig.from_env()
