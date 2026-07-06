import os
from typing import Any
import logging

from dotenv import load_dotenv
from google.generativeai import GenerativeModel

load_dotenv()
logger = logging.getLogger(__name__)


async def generate_summary(payload: dict[str, Any]) -> str:
    """Generate a plain-English summary of an astronomical object.

    Uses the GEMINI_API_KEY environment variable. If you migrate to the
    newer `google-genai` SDK, it can pick up the key automatically and
    simplify this code.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "No summary available."

    try:
        # Use the requested model
        model = GenerativeModel(model_name="gemini-2.5-flash")
        response = model.generate_content(
            f"Explain the following astronomical object data in plain English for a general audience: {payload}"
        )
        return response.text or "No summary available."
    except Exception as e:
        logger.exception("Failed to generate summary: %s", e)
        return "No summary available."
