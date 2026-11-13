"""Extract timed speech and frame text from the staged clip on the hosted runner."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys


def frame_text(frame):
    result = subprocess.run(["tesseract", str(frame), "stdout", "-l", "ukr+eng", "--psm", "11", "tsv"],
                            capture_output=True, text=True, encoding="utf-8", check=True, timeout=12)
    rows = csv.DictReader(io.StringIO(result.stdout), delimiter="\t", quoting=csv.QUOTE_NONE)
    return " ".join(r["text"].strip() for r in rows if r["level"] == "5" and r["text"].strip()
                    and float(r["conf"]) >= 55)


def transcribe(wav, model_path):
    if wav is None:
        return [], None
    import torch
    import whisper
    from silero_vad import get_speech_timestamps, load_silero_vad

    torch.set_num_threads(2)
    samples = whisper.load_audio(str(wav))
    intervals = get_speech_timestamps(torch.from_numpy(samples), load_silero_vad(),
                                      sampling_rate=16000, min_silence_duration_ms=600, speech_pad_ms=150)
    if not intervals:
        return [], None
    voice = [(i["start"] / 16000, i["end"] / 16000) for i in intervals]
    model = whisper.load_model(str(model_path), device="cpu")
    result = model.transcribe(samples, fp16=False, temperature=0, verbose=None, condition_on_previous_text=False)
    segments = []
    for segment in result["segments"]:
        start, end = segment["start"], segment["end"]
        overlap = sum(max(0, min(end, b) - max(start, a)) for a, b in voice)
        text = segment["text"].strip()
        if text and end > start and overlap >= 0.4 * (end - start) and segment["avg_logprob"] >= -1:
            segments.append({"start": start, "end": end, "text": text})
    return segments, result["language"] if segments else None


def analyze(root, work):
    sys.path.insert(0, str(root / "tiktok-video-analyzer"))
    import analyze_tiktok as upstream
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    os.environ["OMP_NUM_THREADS"] = "2"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    video = work / "video.mp4"
    probe = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height",
                                     "-of", "json", str(video)], timeout=10)
    info = json.loads(probe)
    duration = float(info["format"]["duration"])
    if not 0 < duration <= 121 or any(s.get("width", 0) * s.get("height", 0) > 4096 * 2160 for s in info["streams"]):
        return {"status": "outside_video_bounds"}
    wav = upstream.step_audio(video, work)
    speech, language = transcribe(wav, root / "whisper-models/small.pt")
    frames = upstream.step_frames(video, work, 5)
    stamps = upstream.estimate_frame_times(frames, duration)
    screens = [{"seconds_approx": stamps.get(f.name), "text": frame_text(f)} for f in frames]
    screens = [s for s in screens if s["text"]]
    return {"status": "ok" if speech or screens else "no_text_extracted", "speech": speech,
            "language": language, "screen": screens}


if __name__ == "__main__":
    root, work = map(Path, sys.argv[1:])
    (work / "analysis.json").write_text(json.dumps(analyze(root, work)))
