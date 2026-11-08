"""POST /api/ingest: upload a blueprint file, get back blueprint.json plus a review list.

Mount it in the backend (backend/ is Mounish's lane):

    from ingest.router import router as ingest_router
    app.include_router(ingest_router)
"""
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from . import IngestError, ingest_bytes

MAX_UPLOAD_BYTES = 20 * 1024 * 1024

router = APIRouter(tags=["ingest"])


@router.post("/api/ingest")
async def ingest_upload(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "The upload is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Uploads must be under 20 MB.")
    try:
        return await run_in_threadpool(ingest_bytes, data, file.filename or "upload", file.content_type)
    except IngestError as exc:
        raise HTTPException(exc.status, exc.message)
