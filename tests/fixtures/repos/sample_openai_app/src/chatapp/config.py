"""Configuration read from the environment."""

import os

API_KEY = os.environ["OPENAI_API_KEY"]
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
TIMEOUT = os.environ.get("REQUEST_TIMEOUT", "30")
