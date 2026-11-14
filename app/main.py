from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Almanac Grid")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
