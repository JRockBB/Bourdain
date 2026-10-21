"""Anthropic vision extraction through an injectable structured-response client."""

import base64
import hashlib
import json
import os
import time
from collections.abc import Callable
from importlib.resources import files
from typing import Any

from pydantic import ValidationError

from bourdain.errors import BourdainError
from bourdain.ingest.extractor import ExtractHints, validate_drafts
from bourdain.ingest.images import PageImage
from bourdain.models import RecipeDraft
from bourdain.models.schemas import Model
from bourdain.normalize.parse import parse_line


class ExtractionResponse(Model):
    recipes: list[RecipeDraft]


class AnthropicExtractor:
    def __init__(
        self,
        model: str,
        *,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ) -> None:
        if not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        self.model, self.client = model, client
        self.sleep, self.max_attempts = sleep, max_attempts
        self.prompt = (
            files("bourdain.ingest")
            .joinpath("prompts/extract-v1.txt")
            .read_text(encoding="utf-8")
        )

    @property
    def cache_identity(self) -> str:
        digest = hashlib.sha256(self.prompt.encode()).hexdigest()
        return f"anthropic:{self.model}:extract-v1:{digest}"

    def extract(self, pages: list[PageImage], hints: ExtractHints) -> list[RecipeDraft]:
        result = self.parse(pages, hints, ExtractionResponse)
        # Derive weights from explicit text, never model estimates.
        for draft in result.recipes:
            for line in draft.ingredients:
                line.source, line.locked = "llm", False
                line.ingredient_id, line.match_confidence, line.grams = (
                    None,
                    None,
                    parse_line(line.raw).grams,
                )
        return validate_drafts(result.recipes, pages, hints)

    def parse[T: Model](
        self, pages: list[PageImage], hints: Model, response_type: type[T]
    ) -> T:
        """Shared bounded structured-image transport for recipes and documents."""
        import anthropic

        if not pages:
            raise BourdainError("No pages supplied for extraction")
        client = self.client
        owned = client is None
        if owned:
            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise BourdainError("Set ANTHROPIC_API_KEY to extract cookbook photos")
            client = anthropic.Anthropic(
                api_key=key, config={}, max_retries=0, timeout=60
            )
        content: list[dict[str, Any]] = [
            {"type": "text", "text": json.dumps(hints.model_dump())}
        ]
        for page in pages:
            content.extend(
                [
                    {"type": "text", "text": f"Page reference: {page.reference}"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(page.content).decode("ascii"),
                        },
                    },
                ]
            )
        try:
            for attempt in range(self.max_attempts):
                try:
                    response = client.messages.parse(
                        model=self.model,
                        max_tokens=8192,
                        system=self.prompt,
                        messages=[{"role": "user", "content": content}],
                        output_format=response_type,
                    )
                    if (
                        response.stop_reason in {"max_tokens", "refusal"}
                        or response.parsed_output is None
                    ):
                        raise BourdainError(
                            "Extraction was incomplete or refused; "
                            "try a smaller page group"
                        )
                    return response_type.model_validate(response.parsed_output)
                except (anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
                    status = getattr(exc, "status_code", None)
                    retryable = (
                        status is None or status in {408, 409, 429} or status >= 500
                    )
                    if not retryable or attempt + 1 == self.max_attempts:
                        reason = f"HTTP {status}" if status else "connection failure"
                        raise BourdainError(
                            f"Anthropic extraction failed ({reason}); "
                            "check credentials/model or retry"
                        ) from None
                    self.sleep(min(2**attempt, 8))
                except (ValidationError, json.JSONDecodeError) as exc:
                    raise BourdainError(
                        "Anthropic returned an invalid extraction; no recipe was saved"
                    ) from exc
        finally:
            if owned:
                client.close()
        raise BourdainError("Extraction did not return a result")
