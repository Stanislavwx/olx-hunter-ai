"""
ai_analyzer.py — Google Gemini multimodal analysis with structured JSON output.

Uses official `google.genai` SDK with automatic retries for transient 503/429 spikes
and enforces strict JSON schema:
  {
    "is_good_deal": bool,
    "confidence":   int  (0-100),
    "extracted_specs": str,
    "reason":       str
  }
"""

import asyncio
import json
import logging
import os
from typing import Any

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# JSON schema enforced via GenerateContentConfig
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_good_deal": {"type": "BOOLEAN"},
        "confidence": {"type": "INTEGER"},
        "extracted_specs": {"type": "STRING"},
        "reason": {"type": "STRING"},
    },
    "required": ["is_good_deal", "confidence", "extracted_specs", "reason"],
}

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    """Lazy-initialise and cache the Gemini client."""
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY is not set in the environment.")
        _client = genai.Client(api_key=api_key)
        logger.info("Gemini client initialised successfully.")
    return _client


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

    lang = os.environ.get("ALERT_LANGUAGE", "ua").strip().lower()
    lang_instruction = (
        "Write your 'extracted_specs' and 'reason' strictly in Ukrainian language (українською мовою)."
        if lang == "ua" else
        "Write your 'extracted_specs' and 'reason' strictly in English."
    )

    return (
        f"LISTING TITLE: {title}\n"
        f"PRICE: {price_str}\n"
        f"LISTING URL: {url}\n"
        f"DESCRIPTION (first 800 chars):\n{description[:800]}\n\n"
        f"---\n"
        f"EXPERT EVALUATION TASK:\n{task_prompt}\n\n"
        f"Analyse the listing text and attached images carefully.\n"
        f"{lang_instruction}\n"
        f"Return ONLY a valid JSON object matching the required schema. "
        f"Be concise in extracted_specs and reason (max 300 chars each)."
    )


async def analyze_offer(
    offer: dict[str, Any],
    image_bytes_list: list[bytes],
    task_prompt: str,
    max_retries: int = 4,
) -> dict[str, Any] | None:
    """
    Send an offer + images to Gemini and return the structured analysis result.

    Includes automatic retry for transient 503 / 429 API spikes.
    """
    try:
        client = _get_client()
        model_name = os.environ.get("GEMINI_MODEL_NAME", "gemini-3.1-flash-lite-preview")
        prompt_text = _build_prompt(offer, task_prompt)

        # Build contents: prompt + binary images
        contents: list[Any] = [prompt_text]
        for img_bytes in image_bytes_list:
            contents.append(
                types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
            )

        logger.info(
            "Sending offer '%s' to Gemini (%s) with %d image(s) …",
            offer.get("title", "unknown")[:60],
            model_name,
            len(image_bytes_list),
        )

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            temperature=0.2,
            max_output_tokens=1024,
        )

        backoff = 3.0
        response = None
        for attempt in range(1, max_retries + 1):
            try:
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model_name,
                    contents=contents,
                    config=config,
                )
                break
            except Exception as exc:
                err_msg = str(exc)
                if ("503" in err_msg or "429" in err_msg or "UNAVAILABLE" in err_msg) and attempt < max_retries:
                    logger.warning(
                        "Gemini API transient error (%s). Retrying attempt %d/%d in %.1fs …",
                        err_msg[:100], attempt, max_retries, backoff
                    )
                    await asyncio.sleep(backoff)
                    backoff *= 2.0
                elif ("404" in err_msg or "NOT_FOUND" in err_msg) and model_name != "gemini-3.1-flash-lite-preview":
                    logger.warning(
                        "Model '%s' not found (404). Automatically falling back to 'gemini-3.1-flash-lite-preview' …",
                        model_name,
                    )
                    model_name = "gemini-3.1-flash-lite-preview"
                    os.environ["GEMINI_MODEL_NAME"] = "gemini-3.1-flash-lite-preview"
                    continue
                else:
                    raise

        if not response or not response.text:
            logger.error("Empty response from Gemini")
            return None

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
        logger.error("Gemini returned non-JSON response: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error during Gemini analysis: %s", exc)
        return None
