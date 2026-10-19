"""Shared object cleanup for individual jobs and storage-first account deletion."""

import logging

logger = logging.getLogger(__name__)


async def delete_job_storage(storage, job, *, best_effort=False):
    keys = list(filter(None, (job.output_key, job.thumbnail_key)))
    # Edits borrow a source; deleting one must not erase another video's output.
    if job.operations is None:
        keys.insert(0, job.source_key)
    prefixes = []
    if job.hls_key:
        prefixes.append(job.hls_key.rsplit("/", 1)[0] + "/")
        # The seek-bar preview beside the MP4 (app.worker.preview). Only ever
        # made after the ladder, so a video without one has none either.
        if job.output_key:
            prefixes.append(job.output_key.rsplit("/", 1)[0] + "/preview/")
    for prefix in prefixes:
        try:
            keys.extend(name for name, _ in await storage.list_objects(prefix))
        except Exception:
            if not best_effort:
                raise
            logger.warning("job %s: could not list objects under %s", job.id, prefix)
    for key in dict.fromkeys(keys):
        try:
            await storage.delete_object(key)
        except Exception:
            if not best_effort:
                raise
            logger.warning("job %s: could not delete storage object %s", job.id, key)
