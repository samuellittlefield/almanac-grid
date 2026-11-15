from __future__ import annotations

from fastapi import FastAPI

from app.api import router as api_router
from app.web import router as web_router

app = FastAPI(title="Almanac Grid")
app.include_router(api_router)
app.include_router(web_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
