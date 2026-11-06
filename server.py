"""Локальный HTTP-сервер: принимает запись, запускает пайплайн в отдельном процессе,
отдаёт прогресс и результат, рендерит экспорт."""
import multiprocessing as mp
import queue
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import export
import worker

STAGES = ["convert", "load", "transcribe", "align", "diarize"]
_ctx = mp.get_context("spawn")


class Job:
    def __init__(self, filename: str, opts: dict):
        self.id = uuid.uuid4().hex[:12]
        self.filename = filename
        self.opts = opts
        self.dir = Path(tempfile.mkdtemp(prefix="rasshifrovka_"))
        self.state = "queued"  # queued | running | done | error | cancelled
        self.stage = None
        self.progress = {s: 0.0 for s in STAGES}
        self.started = time.time()
        self.finished = None
        self.error = None
        self.warnings = []
        self.result = None
        self.proc = None
        self.q = None

    def public(self) -> dict:
        return {
            "id": self.id, "filename": self.filename, "state": self.state, "stage": self.stage,
            "progress": self.progress, "elapsed": (self.finished or time.time()) - self.started,
            "error": self.error, "warnings": self.warnings, "result": self.result, "opts": self.opts,
        }

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.items: dict[str, Job] = {}

    def active(self):
        with self.lock:
            return next((j for j in self.items.values() if j.state in ("queued", "running")), None)

    def start(self, job: Job, input_path: Path):
        job.q = _ctx.Queue()
        job.proc = _ctx.Process(target=worker.run, daemon=True,
                                args=(str(job.dir), str(input_path), job.opts, config.load(), job.q))
        with self.lock:
            self.items[job.id] = job
        job.state = "running"
        job.proc.start()
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()

    def _pump(self, job: Job):
        while job.state == "running":
            try:
                msg = job.q.get(timeout=0.5)
            except queue.Empty:
                if not job.proc.is_alive() and job.state == "running":
                    job.state, job.error = "error", "Процесс расшифровки неожиданно завершился (возможно, не хватило памяти)"
                continue
            t = msg.get("type")
            if t == "stage":
                job.stage = msg["stage"]
            elif t == "progress":
                job.progress[msg["stage"]] = round(float(msg["value"]), 1)
            elif t == "warning":
                job.warnings.append(msg["message"])
            elif t == "done":
                job.result = {"blocks": msg["blocks"], "language": msg.get("language"), "duration": msg.get("duration")}
                job.state = "done"
            elif t == "error":
                job.state, job.error = "error", msg["message"]
                print(msg.get("trace", ""), flush=True)
        job.finished = time.time()
        if job.proc.is_alive():
            job.proc.join(timeout=5)
            if job.proc.is_alive():
                job.proc.kill()
        job.cleanup()  # кэш и промежуточные файлы больше не нужны

    def cancel(self, job_id: str):
        job = self.items.get(job_id)
        if not job:
            return
        if job.state == "running":
            job.state = "cancelled"
            if job.proc and job.proc.is_alive():
                job.proc.kill()
        job.cleanup()

    def shutdown(self):
        for j in list(self.items.values()):
            self.cancel(j.id)


jobs = Jobs()
app = FastAPI()


def _opts(language: str, speakers: int, diarize: bool) -> dict:
    return {"language": language or "ru", "speakers": speakers if speakers and speakers > 0 else None,
            "diarize": bool(diarize)}


def _ensure_free():
    if jobs.active():
        raise HTTPException(409, "Уже идёт расшифровка другой записи")


@app.get("/api/info")
def info():
    cfg = config.load()
    return {"model": cfg["model_size"], "device": cfg["device"], "compute": cfg["compute_type"],
            "language": cfg["default_language"], "has_token": bool(cfg.get("hf_token")),
            "ffmpeg": bool(shutil.which("ffmpeg"))}


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...), language: str = Form("ru"),
                     speakers: int = Form(0), diarize: bool = Form(True)):
    _ensure_free()
    job = Job(file.filename or "запись", _opts(language, speakers, diarize))
    dst = job.dir / ("input" + Path(file.filename or "").suffix)
    with open(dst, "wb") as out:
        shutil.copyfileobj(file.file, out, length=4 * 1024 * 1024)
    jobs.start(job, dst)
    return job.public()


class PathJob(BaseModel):
    path: str
    language: str = "ru"
    speakers: int = 0
    diarize: bool = True


@app.post("/api/jobs/by-path")
def create_job_by_path(body: PathJob):
    """Файл выбран через нативный диалог: копируем без загрузки через браузер."""
    _ensure_free()
    src = Path(body.path).expanduser()
    if not src.is_file():
        raise HTTPException(404, "Файл не найден")
    job = Job(src.name, _opts(body.language, body.speakers, body.diarize))
    dst = job.dir / ("input" + src.suffix)
    shutil.copyfile(src, dst)
    jobs.start(job, dst)
    return job.public()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.items.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return job.public()


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str):
    jobs.cancel(job_id)
    return {"ok": True}


class ExportBody(BaseModel):
    format: str
    title: str = "Расшифровка"
    timestamps: bool = True
    speakers: dict = {}
    blocks: list = []


@app.post("/api/export")
def export_doc(body: ExportBody):
    if body.format not in export.FORMATS:
        raise HTTPException(400, "Неизвестный формат")
    data = export.render(body.format, body.model_dump())
    name = quote(f"{body.title}.{body.format}")
    return Response(data, media_type=export.FORMATS[body.format],
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})


@app.get("/")
def index():
    return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
