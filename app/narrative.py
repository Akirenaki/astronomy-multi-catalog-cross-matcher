import logging
import os
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
from google import genai
from google.genai.errors import APIError  # Clean error handling

logger = logging.getLogger(__name__)

# 1. Use reliable, deterministic paths to load the environment first
ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=ROOT_DIR / ".env")


# 2. Safely initialize the global client without breaking on missing keys
def _init_client() -> genai.Client | None:
    if not os.getenv("GEMINI_API_KEY"):
        return None
    try:
        return genai.Client()
    except Exception as e:
        logger.error("Failed to initialize Gemini client: %s", e)
        return None


client = _init_client()


async def generate_summary(payload: dict[str, Any]) -> str:
    """Generate a plain-English summary of an astronomical object."""
    if not client:
        logger.warning(
            "GEMINI_API_KEY is not set -- skipping narrative generation. "
            "Returning default 'No summary available.' message."
        )
        return "No summary available."

    try:
        # Uses the fast, pre-warmed connection pool from the global client
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",  # note: gemini-2.5-flash is the stable production flash model
            contents=f"Explain the following astronomical object data in plain English: {payload}",
        )
        return response.text or "No summary available."
    except APIError as e:
        # Catch specific SDK/API errors first
        logger.error("Gemini API error occurred: %s", e)
        return "No summary available."
    except Exception as e:
        logger.exception("Unexpected failure while generating summary: %s", e)
        return "No summary available."
