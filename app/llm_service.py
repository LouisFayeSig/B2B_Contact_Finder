from __future__ import annotations

import json
import logging
import ssl
from collections.abc import Mapping
from typing import Any, Protocol, TYPE_CHECKING

from tenacity import Retrying, before_sleep_log, stop_after_attempt, wait_fixed

from app.config import AppConfig
from app.models import CompanyResult, CompanyRow
from app.prompts import SYSTEM_PROMPT, build_company_search_prompt
from app.utils import sanitize_result, try_parse_json

if TYPE_CHECKING:
    from openai import OpenAI


class CompanySearchService(Protocol):
    def search_company_contact(self, company: CompanyRow) -> CompanyResult:
        ...


RESULT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "email": {"type": "string"},
        "phone": {"type": "string"},
        "website": {"type": "string"},
    },
    "required": ["email", "phone", "website"],
    "additionalProperties": False,
}

DEFAULT_MAX_OUTPUT_TOKENS = 600
RETRY_MAX_OUTPUT_TOKENS = 1200


class OpenAIWebSearchService:
    def __init__(self, config: AppConfig, logger: Any) -> None:
        self._config = config
        self._logger = logger
        self._client = self._build_client()

    def search_company_contact(self, company: CompanyRow) -> CompanyResult:
        response = self._request_with_token_retry(company)

        response_text = self._extract_text_response(response)
        if not response_text:
            self._logger.warning(
                "Reponse vide ou non exploitable pour la ligne %s (%s).",
                company.row_index,
                company.company_name,
            )
            return CompanyResult.not_found()

        return self._parse_json_response(response_text)

    def _request_with_token_retry(self, company: CompanyRow) -> Any:
        request_payload = self._build_request(company, max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS)

        try:
            response = self._perform_request(request_payload)
            if not self._should_retry_for_max_output_tokens(response):
                return response

            self._logger.warning(
                "Reponse incomplete pour la ligne %s (%s) a cause de max_output_tokens, nouvelle tentative.",
                company.row_index,
                company.company_name,
            )
            expanded_payload = self._build_request(
                company,
                max_output_tokens=RETRY_MAX_OUTPUT_TOKENS,
            )
            return self._perform_request(expanded_payload)
        except Exception:
            self._logger.exception(
                "Echec API OpenAI apres retries pour la ligne %s (%s).",
                company.row_index,
                company.company_name,
            )
            raise

    def _build_client(self) -> "OpenAI":
        from openai import DefaultHttpxClient, OpenAI

        http_client = DefaultHttpxClient(
            verify=self._build_ssl_context(),
            timeout=self._config.request_timeout,
        )

        # Les signatures du SDK evoluent. On tente d'abord l'initialisation la plus
        # complete, puis on degrade proprement pour rester compatible.
        try:
            return OpenAI(
                api_key=self._config.openai_api_key,
                http_client=http_client,
                timeout=self._config.request_timeout,
                max_retries=0,
            )
        except TypeError:
            try:
                return OpenAI(
                    api_key=self._config.openai_api_key,
                    http_client=http_client,
                    timeout=self._config.request_timeout,
                )
            except TypeError:
                return OpenAI(
                    api_key=self._config.openai_api_key,
                    http_client=http_client,
                )

    def _build_request(self, company: CompanyRow, *, max_output_tokens: int) -> dict[str, Any]:
        user_prompt = build_company_search_prompt(company)
        return {
            "model": self._config.openai_model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_prompt}],
                },
            ],
            "tools": [{"type": "web_search"}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "company_contact_result",
                    "strict": True,
                    "schema": RESULT_JSON_SCHEMA,
                }
            },
            "temperature": 0,
            "max_output_tokens": max_output_tokens,
        }

    def _perform_request(self, request_payload: dict[str, Any]) -> Any:
        for attempt in Retrying(
            stop=stop_after_attempt(self._config.max_retries),
            wait=wait_fixed(self._config.retry_wait_seconds),
            before_sleep=before_sleep_log(self._logger, logging.WARNING),
            reraise=True,
        ):
            with attempt:
                return self._client.responses.create(**request_payload)

        raise RuntimeError("Echec inattendu des retries OpenAI.")

    def _extract_text_response(self, response: Any) -> str:
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        payload = self._to_plain_data(response)

        if isinstance(payload, Mapping):
            direct_text = payload.get("output_text")
            if isinstance(direct_text, str) and direct_text.strip():
                return direct_text.strip()

            output_items = payload.get("output")
            if isinstance(output_items, list):
                collected: list[str] = []
                for item in output_items:
                    if not isinstance(item, Mapping):
                        continue
                    content_items = item.get("content") or []
                    if not isinstance(content_items, list):
                        continue
                    for content in content_items:
                        if not isinstance(content, Mapping):
                            continue
                        text_value = content.get("text")
                        if isinstance(text_value, str) and text_value.strip():
                            collected.append(text_value.strip())
                        elif isinstance(text_value, Mapping):
                            nested_value = text_value.get("value")
                            if isinstance(nested_value, str) and nested_value.strip():
                                collected.append(nested_value.strip())
                        json_value = content.get("json")
                        if isinstance(json_value, Mapping):
                            collected.append(json.dumps(json_value, ensure_ascii=False))

                if collected:
                    return "\n".join(collected)

        return ""

    def _parse_json_response(self, response_text: str) -> CompanyResult:
        parsed = try_parse_json(response_text)
        if parsed is None:
            self._logger.warning("Impossible de parser le JSON retourne par le modele.")
            return CompanyResult.not_found()

        try:
            result = CompanyResult.model_validate(parsed)
        except Exception:
            self._logger.warning("JSON present mais schema inexploitable : %s", parsed)
            return CompanyResult.not_found()

        return sanitize_result(result)

    def _to_plain_data(self, value: Any) -> Any:
        if hasattr(value, "model_dump"):
            try:
                return value.model_dump()
            except Exception:
                return value
        if hasattr(value, "to_dict"):
            try:
                return value.to_dict()
            except Exception:
                return value
        return value

    def _build_ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()

        if self._config.openai_ca_bundle is None:
            return context

        ca_bundle_path = self._config.openai_ca_bundle.resolve()
        context.load_verify_locations(cafile=str(ca_bundle_path))

        self._logger.info("Bundle CA personnalise charge pour OpenAI : %s", ca_bundle_path)
        return context

    def _should_retry_for_max_output_tokens(self, response: Any) -> bool:
        payload = self._to_plain_data(response)
        if not isinstance(payload, Mapping):
            return False
        if payload.get("status") != "incomplete":
            return False
        incomplete_details = payload.get("incomplete_details")
        if not isinstance(incomplete_details, Mapping):
            return False
        return incomplete_details.get("reason") == "max_output_tokens"
