"""Robust LLM response parser — extracts structured data from messy model output.

Tries multiple strategies to find valid JSON in LLM responses that may
contain markdown fences, free text preambles, trailing notes, or
non-JSON formats.
"""

import json
import logging
import re

logger = logging.getLogger("slayMetrics.parser")


def extract_json(raw: str) -> dict | list | None:
    """Try multiple strategies to extract JSON from LLM response text.

    Returns the parsed JSON object/array, or None if all strategies fail.
    """
    if not raw or not raw.strip():
        return None

    raw = raw.strip()

    # Strategy 1: Direct parse
    result = _try_direct(raw)
    if result is not None:
        return result

    # Strategy 2: Strip markdown code fences
    result = _try_strip_fences(raw)
    if result is not None:
        return result

    # Strategy 3: Find JSON object { ... } anywhere in text
    result = _try_find_json_object(raw)
    if result is not None:
        return result

    # Strategy 4: Find JSON array [ ... ] anywhere in text
    result = _try_find_json_array(raw)
    if result is not None:
        return result

    # Strategy 5: Strip trailing non-JSON text after closing brace/bracket
    result = _try_strip_trailing(raw)
    if result is not None:
        return result

    logger.debug("All JSON extraction strategies failed for: %s", raw[:200])
    return None


def extract_json_or_text(raw: str) -> dict | list | str:
    """Extract JSON if possible, otherwise return cleaned raw text.

    Never returns None — always returns something usable.
    """
    result = extract_json(raw)
    if result is not None:
        return result
    # Return cleaned text as fallback
    return _clean_text(raw)


def _try_direct(raw: str) -> dict | list | None:
    """Strategy 1: Direct json.loads."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def _try_strip_fences(raw: str) -> dict | list | None:
    """Strategy 2: Remove markdown code fences and parse."""
    if "```" not in raw:
        return None
    # Remove ```json or ``` markers
    cleaned = re.sub(r"```(?:json|JSON)?\s*\n?", "", raw)
    cleaned = cleaned.strip()
    return _try_direct(cleaned)


def _try_find_json_object(raw: str) -> dict | list | None:
    """Strategy 3: Find the outermost { ... } in the text."""
    start = raw.find("{")
    if start == -1:
        return None
    # Find matching closing brace by counting depth
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(raw)):
        c = raw[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def _try_find_json_array(raw: str) -> dict | list | None:
    """Strategy 4: Find the outermost [ ... ] in the text."""
    start = raw.find("[")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(raw)):
        c = raw[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def _try_strip_trailing(raw: str) -> dict | list | None:
    """Strategy 5: Find last } or ] and truncate trailing text."""
    # Try object
    last_brace = raw.rfind("}")
    if last_brace > 0:
        first_brace = raw.find("{")
        if first_brace >= 0:
            try:
                return json.loads(raw[first_brace:last_brace + 1])
            except (json.JSONDecodeError, ValueError):
                pass
    # Try array
    last_bracket = raw.rfind("]")
    if last_bracket > 0:
        first_bracket = raw.find("[")
        if first_bracket >= 0:
            try:
                return json.loads(raw[first_bracket:last_bracket + 1])
            except (json.JSONDecodeError, ValueError):
                pass
    return None


def _clean_text(raw: str) -> str:
    """Clean raw text for use as fallback findings."""
    # Remove markdown fences
    cleaned = re.sub(r"```(?:json|JSON)?\s*\n?", "", raw)
    # Remove leading "Here is" preambles
    cleaned = re.sub(r"^(?:Here is|Below is|The following)[^:]*:\s*\n?",
                     "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()
