"""Talks to the API over raw HTTP. No SDK is installed or imported.

Nothing here resolves to a library call, so name resolution finds nothing. The
endpoint path is the only handle the analyser has.
"""

import os

import httpx

BASE_URL = "https://api.openai.com"


def complete(prompt: str) -> dict:
    response = httpx.post(
        f"{BASE_URL}/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 128,
        },
    )
    return response.json()
