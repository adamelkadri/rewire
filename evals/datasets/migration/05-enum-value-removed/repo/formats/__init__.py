"""Choose an output format for a request."""


def build_payload(prompt: str, structured: bool = False) -> dict[str, object]:
    """Build the JSON body, asking for JSON when structured output is wanted."""
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": "json" if structured else "text",
    }
