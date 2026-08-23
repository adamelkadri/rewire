"""A second module using the SDK through a class attribute."""

from openai import OpenAI


class Summariser:
    def __init__(self) -> None:
        self._client = OpenAI()

    def run(self, text: str) -> str:
        result = self._client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": text}],
            max_tokens=64,
        )
        return result.choices[0].message.content
