"""Request construction."""


def build_payload(prompt: str) -> dict[str, object]:
    """Build the JSON body."""
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "best_of": 3,
    }
