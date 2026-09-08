from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.jobs import router as jobs_router
from app.api.uploads import MAX_FILE_SIZE
from app.api.uploads import router as uploads_router
from app.errors import ApiNotFoundError


def create_app() -> FastAPI:
    application = FastAPI(title="Flickpond API", version="0.1.0")

    @application.middleware("http")
    async def reject_oversized_bodies(request: Request, call_next):
        """Refuse an over-limit upload before its body is read.

        Starlette parses the entire multipart body into a spooled temp file
        while resolving the endpoint's dependencies, so the size check inside
        the handler only runs once the bytes are already on this container's
        disk. Content-Length is client-supplied and so cannot be the only
        check -- the handler still measures what actually arrived -- but it is
        what lets an honestly-declared oversized upload be refused for free.
        """
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_FILE_SIZE:
            return JSONResponse(status_code=413, content={"error": "file too large"})
        return await call_next(request)

    # Added after the size guard, which makes CORS the outermost layer: a 413
    # from that guard still needs its Access-Control-Allow-Origin header, or
    # the browser reports a network error instead of the real reason.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
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
