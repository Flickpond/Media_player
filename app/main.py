from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.jobs import router as jobs_router
from app.api.uploads import router as uploads_router
from app.errors import ApiNotFoundError


def create_app() -> FastAPI:
    application = FastAPI(title="Flickpond API", version="0.1.0")
    application.include_router(jobs_router)
    application.include_router(uploads_router)

    @application.exception_handler(ApiNotFoundError)
    async def not_found_handler(_request: Request, exception: ApiNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": exception.message})

    @application.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
