import json
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4
from zipfile import ZipFile


class BatchJobs:
    def __init__(self, root, engine):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.engine = engine
        self.stopping = Event()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cosyvoice-batch")
        for status_path in sorted(self.root.glob("*/status.json"), key=lambda path: path.stat().st_mtime):
            status = self.get(status_path.parent.name)
            if status and status["task_id"] == status_path.parent.name and status["status"] in {"queued", "running"}:
                self.worker.submit(self.run, status_path.parent)

    def close(self):
        self.stopping.set()
        self.worker.shutdown(cancel_futures=True)

    def get(self, task_id):
        path = self.root / str(task_id) / "status.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def archive_path(self, task_id):
        return self.root / str(task_id) / "audio.zip"

    def submit(self, entries, voices, transcript, audio):
        task_id = str(uuid4())
        status = {
            "task_id": task_id, "status": "queued", "completed": 0,
            "total": len(entries), "error": None,
        }
        path = self.root / task_id
        with TemporaryDirectory(prefix=".staging-", dir=self.root) as staging:
            staging = Path(staging)
            request = {"entries": entries, "voices": voices, "transcript": transcript}
            request_path = staging / "request.json"
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            request_path.chmod(0o600)
            if audio is not None:
                prompt_path = staging / "prompt.audio"
                with prompt_path.open("wb") as target:
                    shutil.copyfileobj(audio, target)
                prompt_path.chmod(0o600)
            self.save_status(staging, status)
            staging.rename(path)
        self.worker.submit(self.run, path)
        return status

    @staticmethod
    def save_status(path, status):
        temporary = path / "status.json.tmp"
        temporary.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path / "status.json")

    def run(self, path):
        if self.stopping.is_set():
            return
        status = self.get(path.name)
        partial = path / "audio.zip.part"
        try:
            request = json.loads((path / "request.json").read_text(encoding="utf-8"))
            status.update(status="running", completed=0)
            self.save_status(path, status)
            prompt = (
                self.engine.uploaded_voice(request["transcript"], path / "prompt.audio")
                if request["transcript"] else nullcontext()
            )
            with prompt as uploaded, ZipFile(partial, "w") as archive:
                voices = [uploaded] if uploaded else request["voices"]
                for index, (name, text) in enumerate(request["entries"]):
                    if self.stopping.is_set():
                        return
                    wav = self.engine.wav(text, voices[index % len(voices)])
                    archive.writestr(name, wav)
                    status["completed"] = index + 1
                    self.save_status(path, status)
            partial.chmod(0o644)
            partial.replace(self.archive_path(path.name))
            path.chmod(0o711)  # Nginx can traverse a completed task directory.
            status["status"] = "succeeded"
        except Exception:
            logging.exception("Batch synthesis failed: %s", path.name)
            status.update(status="failed", error="合成失败，请使用任务 ID 查看服务日志")
        finally:
            partial.unlink(missing_ok=True)
            if status["status"] != "running":
                self.save_status(path, status)
                (path / "request.json").unlink(missing_ok=True)
                (path / "prompt.audio").unlink(missing_ok=True)
