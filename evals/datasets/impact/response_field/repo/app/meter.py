"""Reads usage off the response. `completion_tokens` became optional."""

from openai import OpenAI

client = OpenAI()


def cost(prompt: str) -> int:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    # Reading a field that may now be absent: this is what breaks.
    return response.usage.completion_tokens


def cost_by_key(payload: dict) -> int:
    # Same field, read out of a decoded body.
    return payload["usage"]["completion_tokens"]
