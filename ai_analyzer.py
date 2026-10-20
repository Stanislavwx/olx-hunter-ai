"""
ai_analyzer.py — Google Gemini multimodal analysis with structured JSON output.

Sends listing metadata + images to Gemini and enforces a strict response schema:
  {
    "is_good_deal": bool,
    "confidence":   int  (0-100),
    "extracted_specs": str,
    "reason":       str
  }
"""

import json
import logging
import os
from typing import Any

import google.generativeai as genai
from google.generativeai.types import GenerationConfig

logger = logging.getLogger(__name__)

# JSON schema enforced via response_mime_type + response_schema
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_good_deal": {"type": "boolean"},
        "confidence": {"type": "integer"},
        "extracted_specs": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["is_good_deal", "confidence", "extracted_specs", "reason"],
}

_model: genai.GenerativeModel | None = None


def _get_model() -> genai.GenerativeModel:
    """Lazy-initialise and cache the Gemini model instance."""
    global _model
    if _model is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY is not set in the environment.")
        genai.configure(api_key=api_key)

        model_name = os.environ.get("GEMINI_MODEL_NAME", "gemini-2.0-flash")
        _model = genai.GenerativeModel(
            model_name=model_name,
            generation_config=GenerationConfig(
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                temperature=0.2,   # low temp for deterministic, factual evaluation
                max_output_tokens=1024,
            ),
        )
        logger.info("Gemini model initialised: %s", model_name)
    return _model


def _build_prompt(offer: dict[str, Any], task_prompt: str) -> str:
    """Construct the full prompt string from listing metadata + task instructions."""
    title = offer.get("title", "N/A")
    price_info = offer.get("params", [])
    price_str = "N/A"
    for p in price_info:
        if p.get("key") == "price":
            price_str = p.get("value", {}).get("label", "N/A")
            break

    description = offer.get("description", "N/A")
    url = offer.get("url", "N/A")

    return (
        f"LISTING TITLE: {title}\n"
        f"PRICE: {price_str}\n"
        f"LISTING URL: {url}\n"
        f"DESCRIPTION (first 800 chars):\n{description[:800]}\n\n"
        f"---\n"
        f"EXPERT EVALUATION TASK:\n{task_prompt}\n\n"
        f"Analyse the listing text and ALL attached images carefully. "
        f"Return ONLY a valid JSON object matching the required schema. "
        f"Be concise in extracted_specs and reason (max 300 chars each)."
    )


async def analyze_offer(
    offer: dict[str, Any],
    image_bytes_list: list[bytes],
    task_prompt: str,
) -> dict[str, Any] | None:
    """
    Send an offer + images to Gemini and return the structured analysis result.

    Args:
        offer:            Raw OLX offer dict.
        image_bytes_list: Downloaded image bytes (max 4).
        task_prompt:      The per-task AI prompt from config.json.

    Returns:
        Parsed dict with keys: is_good_deal, confidence, extracted_specs, reason.
        Returns None on any error so the daemon continues processing other offers.
    """
    try:
        model = _get_model()
        prompt_text = _build_prompt(offer, task_prompt)

        # Build multimodal content parts: text + image blobs
        content_parts: list[Any] = [prompt_text]
        for img_bytes in image_bytes_list:
            content_parts.append(
                {"mime_type": "image/jpeg", "data": img_bytes}
            )

        logger.info(
            "Sending offer '%s' to Gemini with %d image(s) …",
            offer.get("title", "unknown")[:60],
            len(image_bytes_list),
        )

        response = await model.generate_content_async(content_parts)
        raw_text = response.text.strip()

        result: dict[str, Any] = json.loads(raw_text)

        # Validate required keys
        required = {"is_good_deal", "confidence", "extracted_specs", "reason"}
        missing = required - result.keys()
        if missing:
            logger.error("Gemini response missing keys: %s — raw: %s", missing, raw_text)
            return None

        # Clamp confidence to [0, 100]
        result["confidence"] = max(0, min(100, int(result["confidence"])))

        logger.info(
            "Gemini verdict for '%s': is_good_deal=%s confidence=%d%%",
            offer.get("title", "")[:60],
            result["is_good_deal"],
            result["confidence"],
        )
        return result

    except json.JSONDecodeError as exc:
        logger.error("Gemini returned non-JSON response: %s — %s", exc, response.text if 'response' in dir() else "no response")  # type: ignore[possibly-undefined]
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error during Gemini analysis: %s", exc)
        return None
