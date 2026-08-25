from __future__ import annotations

import json
import logging
import ssl
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_fixed,
)

from app.config import AppConfig
from app.models import NOT_FOUND_LABEL, CompanyResult, CompanyRow
from app.prompts import SYSTEM_PROMPT, build_company_search_prompt
from app.site_extractor import DeterministicSiteExtractor
from app.utils import sanitize_result, try_parse_json

if TYPE_CHECKING:
    from openai import OpenAI


class CompanySearchService(Protocol):
    def search_company_contact(self, company: CompanyRow) -> CompanyResult: ...


class ModelResponseError(RuntimeError):
    """Réponse API reçue mais inutilisable ; la ligne doit rester retraitable."""


RESULT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "email": {"type": "string"},
        "phone": {"type": "string"},
        "website": {"type": "string"},
        "email_source": {"type": "string"},
        "phone_source": {"type": "string"},
        "website_source": {"type": "string"},
        "identity_verified": {"type": "boolean"},
        "identity_match_type": {
            "type": "string",
            "enum": ["siret", "name_and_address", "name_and_city", "none"],
        },
        "identity_source": {"type": "string"},
    },
    "required": [
        "email",
        "phone",
        "website",
        "email_source",
        "phone_source",
        "website_source",
        "identity_verified",
        "identity_match_type",
        "identity_source",
    ],
    "additionalProperties": False,
}

DEFAULT_MAX_OUTPUT_TOKENS = 600
RETRY_MAX_OUTPUT_TOKENS = 1200


class AzureFoundryWebSearchService:
    def __init__(
        self,
        config: AppConfig,
        logger: Any,
        *,
        site_extractor: DeterministicSiteExtractor | None = None,
    ) -> None:
        self._config = config
        self._logger = logger
        self._client = self._build_client()
        self._site_extractor = site_extractor or DeterministicSiteExtractor(config, logger)

    def search_company_contact(self, company: CompanyRow) -> CompanyResult:
        response = self._request_with_token_retry(company)

        response_text = self._extract_text_response(response)
        if not response_text:
            self._logger.warning(
                "Reponse vide ou non exploitable pour la ligne %s (%s).",
                company.row_index,
                company.company_name,
            )
            raise ModelResponseError("Réponse Azure Foundry vide ou non exploitable.")

        result = self._parse_json_response(response_text)
        sources = self._extract_web_search_sources(response) if self._config.search_audit_enabled else []
        result = self._attach_sources(result, sources)
        result = self._mark_llm_extraction_methods(result)

        site_extractor = getattr(self, "_site_extractor", None)
        if self._config.site_extraction_enabled and site_extractor is not None:
            try:
                result = site_extractor.enrich(company, result)
            except Exception as exc:
                self._logger.warning(
                    "Ligne %s : extraction directe du site ignoree apres erreur : %s",
                    company.row_index,
                    exc,
                )
        consulted_sources = list(dict.fromkeys([*sources, *result.deterministic_pages]))
        result = self._apply_audit_evidence_policy(result, consulted_sources)

        response_payload = self._to_plain_data(response)
        response_id = self._read_response_string(response_payload, "id")
        model_snapshot = self._read_response_string(response_payload, "model")
        usage = self._extract_usage(response_payload)
        return sanitize_result(
            {
                **result.model_dump(),
                "sources": result.sources,
                "searched_at_utc": datetime.now(UTC),
                "model_deployment": self._config.azure_foundry_model_deployment,
                "model_snapshot": model_snapshot,
                "response_id": response_id,
                **usage,
                "web_search_calls": self._count_web_search_calls(response_payload),
            }
        )

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
                "Echec API Azure Foundry apres retries pour la ligne %s (%s).",
                company.row_index,
                company.company_name,
            )
            raise

    def _build_client(self) -> OpenAI:
        from openai import DefaultHttpxClient, OpenAI

        api_key: Any = self._config.azure_foundry_api_key
        if not api_key:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            api_key = get_bearer_token_provider(
                DefaultAzureCredential(),
                "https://ai.azure.com/.default",
            )

        http_client = DefaultHttpxClient(
            verify=self._build_ssl_context(),
            timeout=self._config.request_timeout,
        )

        # Les signatures du SDK evoluent. On tente d'abord l'initialisation la plus
        # complete, puis on degrade proprement pour rester compatible.
        try:
            return OpenAI(
                api_key=api_key,
                base_url=self._config.azure_foundry_endpoint,
                http_client=http_client,
                timeout=self._config.request_timeout,
                max_retries=0,
            )
        except TypeError:
            try:
                return OpenAI(
                    api_key=api_key,
                    base_url=self._config.azure_foundry_endpoint,
                    http_client=http_client,
                    timeout=self._config.request_timeout,
                )
            except TypeError:
                return OpenAI(
                    api_key=api_key,
                    base_url=self._config.azure_foundry_endpoint,
                    http_client=http_client,
                )

    def _build_request(self, company: CompanyRow, *, max_output_tokens: int) -> dict[str, Any]:
        user_prompt = build_company_search_prompt(company)
        web_search_tool: dict[str, Any] = {"type": "web_search"}
        if self._config.web_search_context_size != "default":
            web_search_tool["search_context_size"] = self._config.web_search_context_size
        request: dict[str, Any] = {
            "model": self._config.azure_foundry_model_deployment,
            "instructions": SYSTEM_PROMPT,
            "input": user_prompt,
            "tools": [web_search_tool],
            "tool_choice": "required",
            "reasoning": {
                "effort": self._config.azure_foundry_reasoning_effort,
            },
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "company_contact_result",
                    "strict": True,
                    "schema": RESULT_JSON_SCHEMA,
                }
            },
            "max_output_tokens": max_output_tokens,
        }
        if self._config.search_audit_enabled:
            request["include"] = ["web_search_call.action.sources"]
        return request

    def _mark_llm_extraction_methods(self, result: CompanyResult) -> CompanyResult:
        payload = result.model_dump()
        if result.email != NOT_FOUND_LABEL:
            payload["email_extraction_method"] = "llm_web_search"
        if result.phone != NOT_FOUND_LABEL:
            payload["phone_extraction_method"] = "llm_web_search"
        if result.identity_verified:
            payload["identity_extraction_method"] = "llm_web_search"
        return CompanyResult.model_validate(payload)

    def _attach_sources(self, result: CompanyResult, sources: list[str]) -> CompanyResult:
        if not sources:
            return result
        payload = result.model_dump()
        payload["sources"] = sources
        return CompanyResult.model_validate(payload)

    def _perform_request(self, request_payload: dict[str, Any]) -> Any:
        for attempt in Retrying(
            stop=stop_after_attempt(self._config.max_retries),
            wait=wait_fixed(self._config.retry_wait_seconds),
            before_sleep=before_sleep_log(self._logger, logging.WARNING),
            retry=retry_if_exception(self._is_retryable_exception),
            reraise=True,
        ):
            with attempt:
                return self._client.responses.create(**request_payload)

        raise RuntimeError("Echec inattendu des retries Azure Foundry.")

    def _is_retryable_exception(self, exception: BaseException) -> bool:
        from openai import APIConnectionError, APITimeoutError, RateLimitError

        if isinstance(
            exception,
            APIConnectionError | APITimeoutError | RateLimitError,
        ):
            return True
        status_code = getattr(exception, "status_code", None)
        return isinstance(status_code, int) and status_code >= 500

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
            raise ModelResponseError("JSON Azure Foundry impossible à parser.")

        try:
            result = CompanyResult.model_validate(parsed)
        except Exception as exc:
            self._logger.warning("JSON present mais schema inexploitable : %s", parsed)
            raise ModelResponseError("Schéma JSON Azure Foundry inexploitable.") from exc

        return sanitize_result(result)

    def _extract_web_search_sources(self, response: Any) -> list[str]:
        payload = self._to_plain_data(response)
        if not isinstance(payload, Mapping):
            return []
        output_items = payload.get("output")
        if not isinstance(output_items, list):
            return []

        sources: list[str] = []
        for item in output_items:
            if not isinstance(item, Mapping):
                continue
            action = item.get("action")
            if isinstance(action, Mapping):
                self._collect_source_urls(action.get("sources"), sources)

            content_items = item.get("content")
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                if not isinstance(content, Mapping):
                    continue
                self._collect_source_urls(content.get("annotations"), sources)
        return sources[:20]

    def _apply_audit_evidence_policy(
        self,
        result: CompanyResult,
        consulted_sources: list[str],
    ) -> CompanyResult:
        if not self._config.search_audit_enabled:
            return result

        payload = result.model_dump()
        if not self._source_was_consulted(result.identity_source, consulted_sources):
            payload["identity_verified"] = False
            payload["identity_match_type"] = "none"
            payload["identity_source"] = NOT_FOUND_LABEL

        for value_field, source_field in (
            ("email", "email_source"),
            ("phone", "phone_source"),
            ("website", "website_source"),
        ):
            source = getattr(result, source_field)
            if not self._source_was_consulted(source, consulted_sources):
                payload[value_field] = NOT_FOUND_LABEL
                payload[source_field] = NOT_FOUND_LABEL

        return CompanyResult.model_validate(payload)

    def _source_was_consulted(self, source: str, consulted_sources: list[str]) -> bool:
        if source == NOT_FOUND_LABEL:
            return False
        normalized_source = source.split("#", 1)[0].rstrip("/")
        return any(candidate.split("#", 1)[0].rstrip("/") == normalized_source for candidate in consulted_sources)

    def _read_response_string(self, payload: Any, key: str) -> str:
        if not isinstance(payload, Mapping):
            return ""
        value = payload.get(key)
        return value.strip() if isinstance(value, str) else ""

    def _extract_usage(self, payload: Any) -> dict[str, int]:
        if not isinstance(payload, Mapping):
            return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        input_tokens = self._read_non_negative_int(usage.get("input_tokens"))
        output_tokens = self._read_non_negative_int(usage.get("output_tokens"))
        total_tokens = self._read_non_negative_int(usage.get("total_tokens"))
        if total_tokens == 0:
            total_tokens = input_tokens + output_tokens
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }

    def _count_web_search_calls(self, payload: Any) -> int:
        if not isinstance(payload, Mapping):
            return 0
        output_items = payload.get("output")
        if not isinstance(output_items, list):
            return 0

        count = 0
        for item in output_items:
            if not isinstance(item, Mapping) or item.get("type") != "web_search_call":
                continue
            action = item.get("action")
            action_type = action.get("type") if isinstance(action, Mapping) else None
            if action_type in (None, "search"):
                count += 1
        return count

    def _read_non_negative_int(self, value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def _collect_source_urls(self, values: Any, destination: list[str]) -> None:
        if not isinstance(values, list):
            return
        for value in values:
            if not isinstance(value, Mapping):
                continue
            url = value.get("url")
            if not isinstance(url, str):
                citation = value.get("url_citation")
                if isinstance(citation, Mapping):
                    url = citation.get("url")
            if isinstance(url, str) and url.strip() and url not in destination:
                destination.append(url.strip())

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

        if self._config.azure_foundry_ca_bundle is None:
            return context

        ca_bundle_path = self._config.azure_foundry_ca_bundle.resolve()
        context.load_verify_locations(cafile=str(ca_bundle_path))

        self._logger.info(
            "Bundle CA personnalise charge pour Azure Foundry : %s",
            ca_bundle_path,
        )
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
