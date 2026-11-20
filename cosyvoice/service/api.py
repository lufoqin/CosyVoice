import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from random import choice
from tempfile import TemporaryDirectory
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from cosyvoice.service.jobs import BatchJobs
from cosyvoice.service.synthesis import ROOT, Synthesis, validate_prompt
from cosyvoice.service.tables import batch_entries


@asynccontextmanager
async def lifespan(app):
    engine = Synthesis()
    batch_dir = os.environ.get("COSYVOICE_BATCH_DIR") or ROOT / "data" / "cosyvoice" / "batches"
    jobs = BatchJobs(batch_dir, engine)
    app.state.engine = engine
    app.state.jobs = jobs
    app.state.nginx_downloads = os.environ.get("COSYVOICE_NGINX_DOWNLOADS") == "1"
    try:
        yield
    finally:
        jobs.close()


app = FastAPI(title="CosyVoice TTS", lifespan=lifespan)


def voice_request(
    request: Request,
    language: Annotated[Literal["zh", "en"], Form()],
    voice_id: Annotated[str | None, Form()] = None,
    prompt_text: Annotated[str | None, Form()] = None,
    prompt_audio: Annotated[UploadFile | None, File()] = None,
):
    try:
        if prompt_audio is None and prompt_text is None:
            return request.app.state.engine.local_voices(language, voice_id), None, None
        if prompt_audio is None or not prompt_text or not prompt_text.strip():
            raise ValueError("prompt_audio 和 prompt_text 必须一起提供")
        validate_prompt(prompt_audio.file)
        prompt_audio.file.seek(0)
        return [], prompt_text.strip(), prompt_audio
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/voices")
def voices(request: Request):
    return [{"voice_id": voice, "language": lang} for voice, lang in request.app.state.engine.voices.items()]


@app.post("/api/v1/synthesize", responses={200: {"content": {"audio/wav": {}}}}, response_class=Response)
def synthesize(request: Request, text: Annotated[str, Form()], source=Depends(voice_request)):
    if not text.strip():
        raise HTTPException(status_code=422, detail="text 不能为空")
    candidates, transcript, audio = source
    engine = request.app.state.engine
    if audio is None:
        wav = engine.wav(text, choice(candidates))
    else:
        with TemporaryDirectory(prefix="cosyvoice-prompt-") as directory:
            path = Path(directory) / "prompt.audio"
            with path.open("wb") as target:
                shutil.copyfileobj(audio.file, target)
            with engine.uploaded_voice(transcript, path) as voice:
                wav = engine.wav(text, voice)
    return Response(wav, media_type="audio/wav", headers={"Content-Disposition": 'attachment; filename="audio.wav"'})


@app.post("/api/v1/batches", status_code=202)
def submit_batch(request: Request, table: Annotated[UploadFile, File()], source=Depends(voice_request)):
    try:
        entries = batch_entries(table.file, table.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    candidates, transcript, audio = source
    status = request.app.state.jobs.submit(entries, candidates, transcript, audio.file if audio else None)
    return {**status, "status_url": f"/api/v1/batches/{status['task_id']}"}


def require_batch(request, task_id):
    status = request.app.state.jobs.get(task_id)
    if status is None:
        raise HTTPException(status_code=404, detail="找不到批量任务")
    return status


@app.get("/api/v1/batches/{task_id}")
def batch_status(request: Request, task_id: UUID):
    status = require_batch(request, task_id)
    if status["status"] == "succeeded" and request.app.state.jobs.archive_path(task_id).is_file():
        status["download_url"] = f"/api/v1/batches/{task_id}/download"
    return status


def completed_archive(request, task_id):
    status = require_batch(request, task_id)
    if status["status"] != "succeeded":
        raise HTTPException(status_code=409, detail="批量任务尚未成功完成")
    return request.app.state.jobs.archive_path(task_id)


@app.get("/api/v1/batches/{task_id}/download", response_class=Response)
def download_batch(request: Request, task_id: UUID):
    path = completed_archive(request, task_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="ZIP 文件已不存在")
    filename = f"batch-{task_id}.zip"
    if request.app.state.nginx_downloads:
        return Response(media_type="application/zip", headers={
            "X-Accel-Redirect": f"/_cosyvoice_downloads/{task_id}/audio.zip",
            "Content-Disposition": f'attachment; filename="{filename}"',
        })
    return FileResponse(path, media_type="application/zip", filename=filename)


@app.delete("/api/v1/batches/{task_id}/download", status_code=204, response_class=Response)
def delete_batch_archive(request: Request, task_id: UUID):
    completed_archive(request, task_id).unlink(missing_ok=True)
    return Response(status_code=204)
