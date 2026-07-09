import logging
import os
from typing import Any

from dotenv import load_dotenv
from google import genai

# Load environment variables from a local .env file when present.
load_dotenv()
logger = logging.getLogger(__name__)


async def generate_summary(payload: dict[str, Any]) -> str:
    """Generate a plain-English summary of an astronomical object using the Google Gemini API."""
    # Skip the API request entirely when the required key is missing.
    if not os.getenv("GEMINI_API_KEY"):
        return "No summary available."

    try:
        # Create the async Gemini client; it reads the API key from the environment automatically.
        client = genai.Client()

        # Ask Gemini to explain the resolved object data in simple language for a general audience.
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Explain the following astronomical object data in plain English for a general audience: {payload}",
        )
        return response.text or "No summary available."
    except Exception as e:
        # Any network or SDK failure should never crash the app; the UI can fall back to a default message.
        logger.exception("Failed to generate summary: %s", e)
        return "No summary available."