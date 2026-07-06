import os
from typing import Any

from dotenv import load_dotenv
from google.generativeai import GenerativeModel

load_dotenv()


async def generate_summary(payload: dict[str, Any]) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return "No summary available."

    try:
        model = GenerativeModel(model_name="gemini-1.5-flash")
        response = model.generate_content(
            f"Explain the following astronomical object data in plain English for a general audience: {payload}"
        )
        return response.text or "No summary available."
    except Exception:
        return "No summary available."
