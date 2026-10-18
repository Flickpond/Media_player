"""Operator account management, with storage cleanup before any row is deleted."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from fastapi.responses import JSONResponse

from app.api.deps import OperatorUser, SessionDependency
from app.api.multipart_uploads import Storage as MultipartStorageDependency
from app.api.uploads import StorageDependency
from app.errors import ApiNotFoundError
from app.repositories.admin_users import (
    account_contents,
    delete_account_rows,
    list_users_with_job_counts,
)
from app.repositories.uploads import lock_owner
from app.schemas.admin_user import AdminUserResponse
from app.services.job_cleanup import delete_job_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/users", tags=["admin"])


@router.get("", response_model=list[AdminUserResponse])
async def list_accounts(
    _operator: OperatorUser,
    session: SessionDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return await list_users_with_job_counts(session, limit=limit, offset=offset)


@router.delete("/{user_id}", status_code=204)
async def delete_account(
    user_id: UUID,
    operator: OperatorUser,
    session: SessionDependency,
    storage: StorageDependency,
    multipart: MultipartStorageDependency,
):
    if user_id == operator.id:
        return JSONResponse(
            {"error": "an operator cannot delete their own account"}, status_code=409
        )
    try:
        if await lock_owner(session, user_id) is None:
            raise ApiNotFoundError("not found")
        jobs, uploads = await account_contents(session, user_id)
        job_ids = {job.id for job in jobs}
        for upload in uploads:
            if upload.state not in {"ready", "completed", "aborted"}:
                await multipart.abort(upload)
            elif upload.state in {"ready", "completed"} and upload.job_id not in job_ids:
                # Individual job deletion is best-effort in storage. Its durable
                # upload session still identifies a source that cleanup may have missed.
                await storage.delete_object(upload.source_key)
        for job in jobs:
            await delete_job_storage(storage, job)
        await delete_account_rows(session, user_id)
        await session.commit()
    except ApiNotFoundError:
        await session.rollback()
        raise
    except Exception:
        await session.rollback()
        logger.exception("account %s: deletion could not be confirmed", user_id)
        return JSONResponse(
            {"error": "account deletion could not be confirmed; please try again"}, status_code=503
        )
    return Response(status_code=204)
