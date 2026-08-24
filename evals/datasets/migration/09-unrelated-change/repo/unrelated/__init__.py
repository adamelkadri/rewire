"""This package only ever calls the embeddings endpoint."""


def build_embedding_request(text: str) -> dict[str, object]:
    """Build the JSON body for /v1/embeddings."""
    return {"model": "text-embedding-3-small", "input": text}
