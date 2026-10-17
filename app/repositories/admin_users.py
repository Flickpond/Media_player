"""Part B account listing and deletion; existing authentication queries stay separate."""

from sqlalchemy import select

from app.models.user import User


async def list_users_with_job_counts(session, *, limit=50, offset=0):
    from sqlalchemy import func

    from app.models.job import Job

    result = await session.execute(
        select(User.id, User.email, User.role, func.count(Job.id).label("job_count"))
        .outerjoin(Job, Job.owner_id == User.id)
        .group_by(User.id)
        .order_by(User.created_at.desc(), User.id)
        .limit(limit)
        .offset(offset)
    )
    return result.mappings().all()


async def account_contents(session, user_id):
    from app.models.job import Job
    from app.models.upload import UploadSession

    # lock_owner has already blocked new uploads and FK inserts for this account.
    uploads = list((await session.scalars(
        select(UploadSession).where(UploadSession.owner_id == user_id).with_for_update()
    )).all())
    jobs = list((await session.scalars(
        select(Job).where(Job.owner_id == user_id).order_by(Job.id).with_for_update()
    )).all())
    return jobs, uploads


async def delete_account_rows(session, user_id):
    from sqlalchemy import delete

    from app.models.job import Job
    from app.models.upload import UploadSession

    # The caller commits once, only after every storage operation has succeeded.
    await session.execute(delete(UploadSession).where(UploadSession.owner_id == user_id))
    await session.execute(delete(Job).where(Job.owner_id == user_id))
    await session.execute(delete(User).where(User.id == user_id))
