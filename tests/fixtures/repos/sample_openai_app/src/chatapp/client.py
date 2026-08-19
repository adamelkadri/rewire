"""Thin wrapper over the OpenAI SDK.

Three different spellings of the same call live in this file on purpose: an
instance attribute, a module-level client and a plain alias. A text search for
any one of them misses the other two.
"""

import os

import openai as oai
from openai import OpenAI as Client

DEFAULT_MODEL = "gpt-4o-mini"

module_client = Client(api_key=os.environ["OPENAI_API_KEY"])


class ChatClient:
    """Wraps a client instance assigned in __init__ and used in other methods."""

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self._client = Client(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = model

    def generate(self, prompt: str, max_tokens: int = 256) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.0,
        )
        return response.choices[0].message.content

    async def generate_async(self, prompt: str) -> str:
        result = await self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=128,
        )
        return result.choices[0].message.content


def summarise(text: str) -> str:
    """Uses the module-level client through a local alias."""
    client = module_client
    completion = client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[{"role": "system", "content": text}],
        max_tokens=64,
    )
    return completion.choices[0].message.content


def legacy_completion(prompt: str) -> str:
    """Uses the module namespace directly rather than a client object."""
    return oai.completions.create(model="gpt-3.5-turbo-instruct", prompt=prompt, max_tokens=32)


def build_payload(prompt: str) -> dict:
    """Builds a request as a dict, so the field names are string keys."""
    return {
        "model": DEFAULT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 100,
    }
