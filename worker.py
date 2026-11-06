"""Пайплайн расшифровки. Запускается в отдельном процессе, чтобы его можно было
остановить в любой момент и чтобы память моделей гарантированно освобождалась.

Шаги те же, что в transcribe.py: ffmpeg -> whisperx (large-v3) -> выравнивание по словам
-> диаризация pyannote -> склейка реплик по спикерам.
Все промежуточные файлы лежат во временной папке задачи и удаляются сервером после завершения.
"""
import gc
import json
import re
import subprocess
import traceback
from pathlib import Path

SAMPLE_RATE = 16000


def _send(q, **msg):
    q.put(msg)


def _probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def convert(src: Path, dst: Path, q) -> float:
    """Любой аудио/видео формат -> WAV 16 кГц моно (то, что нужно whisper)."""
    duration = _probe_duration(src)
    proc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
         "-progress", "pipe:1", str(dst)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    for line in proc.stdout:
        if line.startswith("out_time_us=") and duration > 0:
            try:
                sec = int(line.split("=", 1)[1]) / 1_000_000
                _send(q, type="progress", stage="convert", value=min(99.0, sec / duration * 100))
            except ValueError:
                pass
    proc.wait()
    if proc.returncode != 0:
        err = proc.stderr.read().strip().splitlines()
        raise RuntimeError("ffmpeg не смог прочитать файл: " + (err[-1] if err else "неизвестная ошибка"))
    return duration


_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?;:…»)])")
_SPACE_AFTER_OPEN = re.compile(r"([«(])\s+")
_MULTISPACE = re.compile(r"\s{2,}")


def normalize(text: str) -> str:
    text = _MULTISPACE.sub(" ", text.strip())
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _SPACE_AFTER_OPEN.sub(r"\1", text)
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def build_blocks(segments: list) -> list:
    """Склеивает подряд идущие сегменты одного спикера в одну реплику (как шаг 4 скрипта)."""
    blocks = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker = seg.get("speaker") or "UNKNOWN"
        start, end = seg.get("start"), seg.get("end")
        if blocks and blocks[-1]["speaker"] == speaker:
            b = blocks[-1]
            b["parts"].append(text)
            if end is not None:
                b["end"] = end
        else:
            blocks.append({"speaker": speaker, "start": start, "end": end, "parts": [text]})
    for b in blocks:
        b["text"] = normalize(" ".join(b.pop("parts")))
    return blocks


def run(job_dir: str, input_path: str, opts: dict, cfg: dict, q) -> None:
    job = Path(job_dir)
    wav = job / "audio.wav"
    try:
        _send(q, type="stage", stage="convert")
        duration = convert(Path(input_path), wav, q)
        Path(input_path).unlink(missing_ok=True)  # исходник больше не нужен
        _send(q, type="progress", stage="convert", value=100, duration=duration)

        _send(q, type="stage", stage="load")
        import whisperx  # тяжёлый импорт, делаем после конвертации

        language = opts.get("language") or None
        if language == "auto":
            language = None
        device = cfg["device"]

        # === ШАГ 1: Транскрипция ===
        model = whisperx.load_model(cfg["model_size"], device, compute_type=cfg["compute_type"],
                                    language=language, threads=int(cfg.get("threads") or 4))
        audio = whisperx.load_audio(str(wav))
        _send(q, type="stage", stage="transcribe")
        result = model.transcribe(
            audio, batch_size=cfg["batch_size"], language=language,
            progress_callback=lambda p: _send(q, type="progress", stage="transcribe", value=p),
        )
        language = result.get("language") or language or "ru"
        del model
        gc.collect()
        _send(q, type="progress", stage="transcribe", value=100, language=language)

        # === ШАГ 2: Выравнивание по словам ===
        _send(q, type="stage", stage="align")
        try:
            model_a, metadata = whisperx.load_align_model(language_code=language, device=device)
            result = whisperx.align(
                result["segments"], model_a, metadata, audio, device, return_char_alignments=False,
                progress_callback=lambda p: _send(q, type="progress", stage="align", value=p),
            )
            del model_a
            gc.collect()
        except Exception as e:  # для редких языков модели выравнивания может не быть
            _send(q, type="warning", message=f"Выравнивание пропущено: {e}")
        _send(q, type="progress", stage="align", value=100)

        # === ШАГ 3: Диаризация ===
        if opts.get("diarize", True):
            _send(q, type="stage", stage="diarize")
            if not cfg.get("hf_token"):
                raise RuntimeError("Для определения спикеров нужен токен Hugging Face: выполните `hf auth login` "
                                   "или пропишите hf_token в app/config.json (см. README)")
            from whisperx.diarize import DiarizationPipeline
            diarize_model = DiarizationPipeline(token=cfg["hf_token"], device=device)
            speakers = opts.get("speakers")
            kw = {"min_speakers": speakers, "max_speakers": speakers} if speakers else {}
            diarize_segments = diarize_model(
                audio, progress_callback=lambda p: _send(q, type="progress", stage="diarize", value=p), **kw,
            )
            result = whisperx.assign_word_speakers(diarize_segments, result)
            del diarize_model
            gc.collect()
            _send(q, type="progress", stage="diarize", value=100)
        else:
            for seg in result["segments"]:
                seg["speaker"] = "SPEAKER_00"

        # === ШАГ 4: Реплики ===
        blocks = build_blocks(result["segments"])
        # JSON round-trip: numpy-числа -> обычные float
        blocks = json.loads(json.dumps(blocks, default=float))
        _send(q, type="done", blocks=blocks, language=language, duration=duration)
    except Exception as e:
        _send(q, type="error", message=str(e) or e.__class__.__name__, trace=traceback.format_exc())
