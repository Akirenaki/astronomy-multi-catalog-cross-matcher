import os
import logging
from typing import Any

from dotenv import load_dotenv
from google import genai

load_dotenv()
logger = logging.getLogger(__name__)


async def generate_summary(payload: dict[str, Any]) -> str:
    """Generate a plain-English summary of an astronomical object using the modern google-genai SDK."""
    # Ensure the API key exists before attempting a call
    if not os.getenv("GEMINI_API_KEY"):
        return "No summary available."

    try:
        # The client automatically picks up GEMINI_API_KEY from the environment
        client = genai.Client()
        
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"Explain the following astronomical object data in plain English for a general audience: {payload}",
        )
        return response.text or "No summary available."
    except Exception as e:
        logger.exception("Failed to generate summary: %s", e)
        return "No summary available."