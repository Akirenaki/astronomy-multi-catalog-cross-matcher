import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

try:
    from google import genai
    from google.genai.errors import APIError  # Clean error handling
except ImportError:  # pragma: no cover - exercised when the optional dependency is absent
    genai = None

    class APIError(Exception):
        """Fallback API error used when google-genai is not installed."""

        pass

logger = logging.getLogger(__name__)


def _init_client() -> Any | None:
    if genai is None:
        return None
    if not os.getenv("GEMINI_API_KEY"):
        return None
    try:
        return genai.Client()
    except Exception as e:
        logger.error("Failed to initialize Gemini client: %s", e)
        return None


client: Any | None = None


def load_environment() -> None:
    """Load environment variables from the repository and app-local .env files."""
    module_path = Path(__file__).resolve()
    app_dir = module_path.parent
    root_dir = module_path.parents[1]

    load_dotenv(dotenv_path=root_dir / ".env", override=False)
    load_dotenv(dotenv_path=app_dir / ".env", override=True)

    global client
    client = _init_client()


load_environment()


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
