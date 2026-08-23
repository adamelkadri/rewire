"""Every call style a real client module uses."""

import os

from openai import OpenAI

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def ask(prompt: str, max_tokens: int = 256) -> str:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content


def ask_with_payload(prompt: str) -> str:
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
    }
    return client.chat.completions.create(**payload).choices[0].message.content
