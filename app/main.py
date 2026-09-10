from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.auth import router as auth_router
from app.api.jobs import admin_router
from app.api.jobs import router as jobs_router
from app.api.uploads import MAX_FILE_SIZE
from app.api.uploads import router as uploads_router
from app.errors import ApiForbiddenError, ApiNotFoundError, ApiUnauthorizedError
from app.services.storage import get_storage_service


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Make sure the bucket exists, once, before serving anything.

    This used to happen on every upload. Doing it here costs one round trip per
    process instead of one per request, and the worker can rely on it because
    compose gates the worker on the API being healthy.

    A failure here is deliberately fatal: an API that cannot reach object
    storage has nothing useful to offer, and failing at startup is far easier
    to diagnose than every upload failing later.
    """
    await get_storage_service().ensure_bucket()
    yield


def create_app() -> FastAPI:
    application = FastAPI(title="Flickpond API", version="0.1.0", lifespan=lifespan)

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
    application.include_router(auth_router)
    application.include_router(jobs_router)
    application.include_router(admin_router)
    application.include_router(uploads_router)

    @application.exception_handler(ApiNotFoundError)
    async def not_found_handler(_request: Request, exception: ApiNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": exception.message})

    @application.exception_handler(ApiUnauthorizedError)
    async def unauthorized_handler(
        _request: Request, exception: ApiUnauthorizedError
    ) -> JSONResponse:
        return JSONResponse(status_code=401, content={"error": exception.message})

    @application.exception_handler(ApiForbiddenError)
    async def forbidden_handler(_request: Request, exception: ApiForbiddenError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"error": exception.message})

    @application.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
