"""HTTP surface."""

from fastapi import FastAPI

from chatapp.client import ChatClient

app = FastAPI(title="chatapp")
chat = ChatClient()


@app.post("/generate")
async def generate(prompt: str) -> dict:
    return {"text": await chat.generate_async(prompt)}
