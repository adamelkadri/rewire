"""The only genuine SDK usage in this repository."""

from openai import OpenAI

client = OpenAI()


def generate(prompt: str) -> str:
    return (
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
        )
        .choices[0]
        .message.content
    )
