from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pathlib import Path
import subprocess
import json
import uuid
import shutil
import os
import re
import html
import base64
import urllib.request
import urllib.error
from typing import Optional

import cv2
import yt_dlp

app = FastAPI(title="MAIGO API", version="20.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
CLIP_DIR = BASE_DIR / "clips"
SHORT_DIR = BASE_DIR / "shorts"

for folder in (UPLOAD_DIR, CLIP_DIR, SHORT_DIR):
    folder.mkdir(parents=True, exist_ok=True)

WINDOWS_FFMPEG = r"C:\Users\Admin\Desktop\New folder\ffmpeg-2026-10-04-git-a35c879992-essentials_build\ffmpeg-2026-10-04-git-a35c879992-essentials_build\bin\ffmpeg.exe"
WINDOWS_FFPROBE = r"C:\Users\Admin\Desktop\New folder\ffmpeg-2026-10-04-git-a35c879992-essentials_build\ffmpeg-2026-10-04-git-a35c879992-essentials_build\bin\ffprobe.exe"
FFMPEG = shutil.which("ffmpeg") or (WINDOWS_FFMPEG if os.path.isfile(WINDOWS_FFMPEG) else "ffmpeg")
FFPROBE = shutil.which("ffprobe") or (WINDOWS_FFPROBE if os.path.isfile(WINDOWS_FFPROBE) else "ffprobe")

# ============================================================
# HOSTED AI BRAIN (GOOGLE GEMINI)
# ============================================================

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
AI_MODEL = "gemini-3.6-flash"


app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
app.mount("/clips", StaticFiles(directory=str(CLIP_DIR)), name="clips")
app.mount("/shorts", StaticFiles(directory=str(SHORT_DIR)), name="shorts")


class AnalyzeRequest(BaseModel):
    url: str


class ClipRequest(BaseModel):
    file_id: str
    start: float
    end: float


class RecommendationRequest(BaseModel):
    file_id: str


class ShortRequest(BaseModel):
    file_id: str
    start: float
    end: float
    attention_video_id: Optional[str] = None
    attention_mode: str = "none"
    smart_framing: bool = True
    caption_style: str = "wordpop"
    caption_position: str = "bottom"
    caption_size: str = "medium"
    hook_overlay: bool = False
    hook_text: str = ""
    resolution: str = "1080"
    quality: str = "balanced"
    background_blur: bool = True
    blur_strength: int = 18


class BestShortRequest(BaseModel):
    file_id: str
    start: float
    end: float
    score: Optional[int] = None
    hook: str = ""
    caption: str = ""
    hashtags: list[str] = list()
    attention_video_id: Optional[str] = None
    attention_mode: str = "none"
    smart_framing: bool = True
    caption_style: str = "wordpop"
    caption_position: str = "bottom"
    caption_size: str = "medium"
    hook_overlay: bool = True
    resolution: str = "1080"
    quality: str = "balanced"
    background_blur: bool = True
    blur_strength: int = 18


class AIChatRequest(BaseModel):
    file_id: Optional[str] = None
    message: str
    recommendations: list[dict] = list()
    settings: dict = dict()


def run_command(command, timeout=600):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Operation timed out.") from exc


def check_tools():
    if not (os.path.isfile(FFMPEG) or shutil.which(FFMPEG)):
        raise RuntimeError(f"FFmpeg not found: {FFMPEG}")
    if not (os.path.isfile(FFPROBE) or shutil.which(FFPROBE)):
        raise RuntimeError(f"FFprobe not found: {FFPROBE}")


def get_video_info(video_path: Path):
    check_tools()
    result = run_command([
        FFPROBE, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(video_path)
    ], 60)
    if result.returncode != 0:
        raise RuntimeError(f"FFprobe failed: {result.stderr}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FFprobe returned invalid JSON.") from exc

    duration = 0.0
    width = 0
    height = 0
    fps = 0.0
    codec = "unknown"

    try:
        duration = float(data.get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        pass

    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if stream:
        width = int(stream.get("width", 0) or 0)
        height = int(stream.get("height", 0) or 0)
        codec = stream.get("codec_name", "unknown")
        try:
            a, b = stream.get("r_frame_rate", "0/1").split("/")
            if float(b) != 0:
                fps = float(a) / float(b)
        except Exception:
            pass

    return {
        "duration_seconds": round(duration, 2),
        "width": width,
        "height": height,
        "fps": round(fps, 2),
        "codec": codec,
    }


def find_video_file(file_id):
    for ext in (".mp4", ".webm", ".mkv", ".mov"):
        p = UPLOAD_DIR / f"{file_id}{ext}"
        if p.exists():
            return p
    return None


def find_subtitle_file(file_id):
    for ext in (".vtt", ".srt"):
        matches = list(UPLOAD_DIR.glob(f"{file_id}*{ext}"))
        if matches:
            return matches[0]
    return None


def metadata_path(file_id):
    return UPLOAD_DIR / f"{file_id}.json"


def save_metadata(file_id, title, source_url="", subtitle_language=""):
    with open(metadata_path(file_id), "w", encoding="utf-8") as f:
        json.dump({
            "file_id": file_id,
            "title": title,
            "source_url": source_url,
            "subtitle_language": subtitle_language,
        }, f, indent=2, ensure_ascii=False)


def load_metadata(file_id):
    path = metadata_path(file_id)
    if not path.exists():
        return {"title": "Video", "source_url": "", "subtitle_language": ""}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"title": "Video", "source_url": "", "subtitle_language": ""}


def choose_subtitle_language(info):
    langs = []
    langs.extend((info.get("subtitles") or {}).keys())
    langs.extend((info.get("automatic_captions") or {}).keys())
    for lang in dict.fromkeys(langs):
        low = lang.lower()
        if low == "en" or low.startswith("en-") or low.startswith("en_"):
            return lang
    return ""


def ts_seconds(value):
    parts = value.strip().replace(",", ".").split(":")
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
    except Exception:
        return 0.0
    return 0.0


def clean_caption(text):
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\{\\.*?\}", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_subtitles(path):
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()]
    except Exception:
        return []

    pattern = re.compile(r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})")
    cues = []
    i = 0
    while i < len(lines):
        match = pattern.search(lines[i])
        if not match:
            i += 1
            continue
        start = ts_seconds(match.group(1))
        end = ts_seconds(match.group(2))
        i += 1
        parts = []
        while i < len(lines) and lines[i] != "":
            cleaned = clean_caption(lines[i])
            if cleaned:
                parts.append(cleaned)
            i += 1
        text = " ".join(parts).strip()
        if text and end > start:
            cues.append({"start": start, "end": end, "text": text})
        i += 1
    return cues


ATTENTION_WORDS = {
    "wait": 4, "what": 3, "why": 3, "how": 2, "look": 3, "watch": 3,
    "bro": 2, "crazy": 4, "insane": 4, "wild": 4, "wow": 4, "nah": 3,
    "finally": 4, "actually": 3, "secret": 5, "hidden": 5, "found": 3,
    "win": 3, "won": 3, "lose": 3, "lost": 3, "fail": 3, "failed": 3,
    "impossible": 5, "best": 3, "worst": 3, "rare": 5, "surprise": 5,
    "surprised": 5, "perfect": 3,
}


def caption_score(text):
    words = re.findall(r"[A-Za-z0-9']+", text.lower())
    score = sum(ATTENTION_WORDS.get(w, 0) for w in words)
    score += text.count("?") * 2
    score += text.count("!") * 2
    return min(20, score)


def detect_scene_changes(video_path):
    check_tools()
    result = run_command([
        FFMPEG, "-hide_banner", "-loglevel", "info", "-i", str(video_path),
        "-vf", "select='gt(scene,0.18)',showinfo", "-an", "-f", "null", os.devnull
    ], 180)
    output = result.stdout + "\n" + result.stderr
    values = []
    for match in re.findall(r"pts_time:([0-9.]+)", output):
        try:
            values.append(round(float(match), 2))
        except ValueError:
            pass
    return sorted(set(values))


def topic_data(title):
    lower = (title or "").lower()
    if "minecraft" in lower:
        return {
            "topic": "Minecraft",
            "hooks": [
                "Bro, wait until you see what happens here 😭",
                "Nahhh, this Minecraft moment was wild 💀",
                "Wait... did that actually just happen? 👀",
            ],
            "captions": [
                "This Minecraft moment took a turn 😭",
                "I did NOT expect this to happen in Minecraft 💀",
                "This might be the wildest part of the video 👀",
            ],
            "hashtags": ["#minecraft", "#minecraftpe", "#gaming", "#shorts", "#maigo"],
        }
    return {
        "topic": "General",
        "hooks": [
            "Wait for the moment everything changes 👀",
            "You are NOT ready for this part 😭",
            "Something crazy happens here 💀",
        ],
        "captions": [
            "This part deserves a short 👀",
            "The moment everything changed 😭",
            "This was not what I expected 💀",
        ],
        "hashtags": ["#shorts", "#fyp", "#viral", "#trending", "#maigo"],
    }


def create_recommendations(duration, scenes, cues, title):
    candidates = []
    for cue in cues:
        score = caption_score(cue["text"])
        if score <= 0:
            continue
        center = (cue["start"] + cue["end"]) / 2
        start = max(0.0, center - 7.0)
        end = min(duration, start + 20.0)
        if end - start < 8:
            continue
        nearby = sum(1 for scene in scenes if abs(scene - center) <= 12)
        candidates.append({
            "start": start, "end": end,
            "score": 55 + score * 2 + min(15, nearby * 4),
            "transcript": True, "scene_count": nearby,
        })

    for scene in scenes:
        start = max(0.0, scene - 7.0)
        end = min(duration, start + 20.0)
        if end - start < 8:
            continue
        nearby = sum(1 for other in scenes if abs(other - scene) <= 12)
        candidates.append({
            "start": start, "end": end,
            "score": 50 + min(20, nearby * 5),
            "transcript": False, "scene_count": nearby,
        })

    if not candidates:
        for point in (0.0, duration * 0.25, duration * 0.5, duration * 0.75):
            start = max(0.0, point)
            end = min(duration, start + 20.0)
            if end - start >= 8:
                candidates.append({
                    "start": start, "end": end, "score": 45,
                    "transcript": False, "scene_count": 0,
                })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected = []
    for candidate in candidates:
        midpoint = (candidate["start"] + candidate["end"]) / 2
        if any(abs(midpoint - ((x["start"] + x["end"]) / 2)) < 12 for x in selected):
            continue
        selected.append(candidate)
        if len(selected) >= 5:
            break

    copy = topic_data(title)
    results = []
    for i, candidate in enumerate(selected):
        if candidate["transcript"]:
            reason = "Spoken content near this point contains attention-grabbing language."
        elif candidate["scene_count"] >= 2:
            reason = "Several visual changes occur close together here."
        else:
            reason = "A noticeable visual change happens near this point."
        results.append({
            "rank": i + 1,
            "start": round(candidate["start"], 2),
            "end": round(candidate["end"], 2),
            "duration": round(candidate["end"] - candidate["start"], 2),
            "score": max(1, min(99, int(round(candidate["score"]))),),
            "reason": reason,
            "topic": copy["topic"],
            "hook": copy["hooks"][i % len(copy["hooks"])],
            "caption": copy["captions"][i % len(copy["captions"])],
            "hashtags": copy["hashtags"],
            "signals": {
                "transcript_signal": candidate["transcript"],
                "nearby_scene_changes": candidate["scene_count"],
            },
        })
    return results



def motion_centers_for_clip(video_path, clip_start, clip_end, crop_width, sample_interval=0.8):
    """Estimate horizontal motion centers for a short clip without a ML model."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []

    try:
        source_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        source_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if source_width <= crop_width or source_height <= 0:
            return []

        max_x = max(0, source_width - crop_width)
        clip_duration = max(0.1, clip_end - clip_start)
        sample_times = []
        t = 0.0
        while t < clip_duration:
            sample_times.append(round(t, 3))
            t += sample_interval
        if not sample_times or sample_times[-1] != round(clip_duration, 3):
            sample_times.append(round(clip_duration, 3))

        results = []
        previous_gray = None
        fallback_center = max_x / 2.0

        for rel_time in sample_times:
            cap.set(cv2.CAP_PROP_POS_MSEC, (clip_start + rel_time) * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                results.append((rel_time, fallback_center))
                continue

            small = cv2.resize(frame, (320, max(1, int(frame.shape[0] * 320 / frame.shape[1]))))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)

            center = fallback_center

            if previous_gray is not None and previous_gray.shape == gray.shape:
                diff = cv2.absdiff(previous_gray, gray)
                _, mask = cv2.threshold(diff, 28, 255, cv2.THRESH_BINARY)
                kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
                mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
                mask = cv2.dilate(mask, kernel, iterations=1)

                contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                candidates = []
                small_w = gray.shape[1]
                scale_x = source_width / float(small_w)

                for contour in contours:
                    area = cv2.contourArea(contour)
                    if area < 80:
                        continue
                    x, y, w, h = cv2.boundingRect(contour)
                    if w < 4 or h < 4:
                        continue
                    candidates.append((area, x + w / 2.0))

                if candidates:
                    candidates.sort(reverse=True)
                    top = candidates[:8]
                    total_area = sum(a for a, _ in top)
                    if total_area > 0:
                        weighted_small_x = sum(a * x for a, x in top) / total_area
                        motion_x = weighted_small_x * scale_x
                        center = motion_x - crop_width / 2.0

            center = max(0.0, min(float(max_x), float(center)))
            results.append((rel_time, center))
            previous_gray = gray
            fallback_center = center

        if not results:
            return []

        # Smooth the path so framing doesn't jerk.
        smoothed = []
        radius = 2
        for i, (time_value, center_value) in enumerate(results):
            lo = max(0, i - radius)
            hi = min(len(results), i + radius + 1)
            avg = sum(results[j][1] for j in range(lo, hi)) / (hi - lo)
            smoothed.append((time_value, avg))

        return smoothed

    finally:
        cap.release()


def make_crop_x_expression(points, max_x):
    """Create a piecewise-linear FFmpeg crop x expression."""
    if not points:
        return str(round(max_x / 2.0, 3))

    if len(points) == 1:
        return str(round(max(0.0, min(max_x, points[0][1])), 3))

    expression = str(round(max(0.0, min(max_x, points[-1][1])), 3))

    for i in range(len(points) - 2, -1, -1):
        t0, x0 = points[i]
        t1, x1 = points[i + 1]
        dt = max(0.001, t1 - t0)
        segment = (
            f"if(lt(t,{t1:.3f}),"
            f"({x0:.3f}+({x1 - x0:.3f})*(t-{t0:.3f})/{dt:.3f}),"
            f"{expression})"
        )
        expression = segment

    return expression


def build_motion_crop_filter(video_path, clip_start, clip_end, target_ratio):
    """Return a crop+scale filter with motion-aware horizontal framing."""
    info = get_video_info(Path(video_path))
    width = info["width"]
    height = info["height"]

    if width <= 0 or height <= 0:
        raise RuntimeError("Could not determine source dimensions for smart framing.")

    crop_width = int(round(height * target_ratio))
    crop_width = max(2, min(width, crop_width))

    target_w = 1080
    target_h = int(round(target_w / target_ratio))

    if width <= crop_width:
        return (
            f"scale={target_w}:{target_h}:"
            "force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h}"
        )

    points = motion_centers_for_clip(
        video_path,
        clip_start,
        clip_end,
        crop_width,
    )

    max_x = width - crop_width
    x_expr = make_crop_x_expression(points, max_x)

    # FFmpeg filter arguments use commas as separators, so commas
    # inside the nested if() expression must be escaped.
    safe_x_expr = x_expr.replace(",", r"\,")

    return (
        f"crop={crop_width}:{height}:{safe_x_expr}:0,"
        f"scale={target_w}:{target_h}"
    )

def ass_time(seconds):
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole = int(seconds % 60)
    centi = int(round((seconds - int(seconds)) * 100))
    if centi >= 100:
        whole += 1
        centi = 0
    if whole >= 60:
        minutes += 1
        whole -= 60
    if minutes >= 60:
        hours += 1
        minutes -= 60
    return f"{hours}:{minutes:02d}:{whole:02d}.{centi:02d}"


def ass_escape(text):
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def create_ass(subtitle_path, clip_start, clip_end, output_path, style="wordpop", position="bottom", size="medium", hook_text="", hook_overlay=False):
    """Create ASS captions using selectable MAIGO visual presets."""

    presets = {
        "wordpop": {"font":"Arial Black","size":54,"primary":"&H00FFFFFF&","secondary":"&H0000FFFF&","outline":"&H00101010&","back":"&H99000000&","bold":1,"outline_width":4,"shadow":3,"margin_v":160},
        "classic": {"font":"Arial","size":52,"primary":"&H00FFFFFF&","secondary":"&H00FFFFFF&","outline":"&H00101010&","back":"&H99000000&","bold":1,"outline_width":3,"shadow":2,"margin_v":160},
        "punchy": {"font":"Impact","size":58,"primary":"&H00FFFFFF&","secondary":"&H0000FFFF&","outline":"&H00000000&","back":"&HCC000000&","bold":1,"outline_width":5,"shadow":4,"margin_v":175},
        "clean": {"font":"Trebuchet MS","size":50,"primary":"&H00FFFFFF&","secondary":"&H00FFFFFF&","outline":"&H00151515&","back":"&H88000000&","bold":1,"outline_width":3,"shadow":2,"margin_v":150},
        "neon": {"font":"Arial Black","size":52,"primary":"&H00FFFFFF&","secondary":"&H00FFFF00&","outline":"&H00FF00FF&","back":"&H99000000&","bold":1,"outline_width":4,"shadow":2,"margin_v":165},
        "meme": {"font":"Impact","size":60,"primary":"&H00FFFFFF&","secondary":"&H0000FFFF&","outline":"&H00000000&","back":"&HAA000000&","bold":1,"outline_width":6,"shadow":3,"margin_v":180},
        "typewriter": {"font":"Courier New","size":44,"primary":"&H00FFFFFF&","secondary":"&H00FFFFFF&","outline":"&H00101010&","back":"&H99000000&","bold":0,"outline_width":2,"shadow":2,"margin_v":155},
        "minimal": {"font":"Arial","size":46,"primary":"&H00FFFFFF&","secondary":"&H00FFFFFF&","outline":"&H00000000&","back":"&H66000000&","bold":0,"outline_width":1,"shadow":1,"margin_v":145},
    }

    if style == "off":
        return 0
    if style not in presets:
        style = "wordpop"

    cfg = dict(presets[style])
    size_scale = {"small": 0.86, "medium": 1.0, "large": 1.18}.get(size, 1.0)
    cfg["size"] = max(20, int(round(cfg["size"] * size_scale)))
    alignment = {"top": 8, "middle": 5, "bottom": 2}.get(position, 2)
    cues = parse_subtitles(subtitle_path)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n"
        "WrapStyle: 2\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: MAIGO,{cfg['font']},{cfg['size']},{cfg['primary']},{cfg['secondary']},{cfg['outline']},{cfg['back']},{cfg['bold']},0,0,0,100,100,0,0,1,{cfg['outline_width']},{cfg['shadow']},{alignment},70,70,{cfg['margin_v']},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    count = 0

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)

        if hook_overlay and hook_text.strip():
            safe_hook = ass_escape(hook_text.strip())[:140]
            # Brief opening hook banner; keep it separate from source subtitles.
            f.write(
                f"Dialogue: 0,0:00:00.00,0:00:02.20,MAIGO,,0,0,0,,{{\\an8\\fad(120,120)\\fscx108\\fscy108\\t(0,180,\\fscx100\\fscy100)}}{safe_hook}\n"
            )

        for cue in cues:
            if cue["end"] <= clip_start or cue["start"] >= clip_end:
                continue

            start = max(0.0, cue["start"] - clip_start)
            end = min(clip_end, cue["end"]) - clip_start
            if end <= start:
                continue

            text = ass_escape(cue["text"])

            if style in {"classic", "minimal", "typewriter"}:
                if style == "classic":
                    animated = r"{\fscx88\fscy88\t(0,180,\fscx100\fscy100)\fad(80,70)}" + text
                else:
                    animated = r"{\fad(50,60)}" + text
                f.write(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},MAIGO,,0,0,0,,{animated}\n")
                count += 1
                continue

            words = re.findall(r"\S+", cue["text"].strip())
            if not words:
                continue

            weights = [max(1, len(re.sub(r"[^A-Za-z0-9']", "", w))) for w in words]
            total_weight = sum(weights)
            cue_duration = end - start
            cursor = start

            for idx, word in enumerate(words):
                duration = cue_duration * weights[idx] / total_weight
                word_start = cursor
                word_end = end if idx == len(words)-1 else min(end, cursor + duration)
                cursor = word_end
                rendered = []

                for i, visible in enumerate(words):
                    safe = ass_escape(visible)
                    if i == idx:
                        if style == "neon":
                            safe = r"{\c&H00FFFF&\fscx112\fscy112\t(0,100,\fscx100\fscy100)}" + safe + r"{\c&H00FFFFFF&}"
                        elif style in {"punchy", "meme"}:
                            safe = r"{\c&H0000FFFF&\fscx115\fscy115\t(0,110,\fscx100\fscy100)}" + safe + r"{\c&H00FFFFFF&}"
                        elif style == "clean":
                            safe = r"{\c&H0000FFFF&\fscx106\fscy106\t(0,90,\fscx100\fscy100)}" + safe + r"{\c&H00FFFFFF&}"
                        else:
                            safe = r"{\c&H0000FFFF&\fscx108\fscy108\t(0,90,\fscx100\fscy100)}" + safe + r"{\c&H00FFFFFF&}"
                    rendered.append(safe)

                line = " ".join(rendered)
                f.write(f"Dialogue: 0,{ass_time(word_start)},{ass_time(word_end)},MAIGO,,0,0,0,,{{\\fad(20,20)}}{line}\n")
                count += 1

    return count

def escape_filter_path(path):
    return str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def normalize_downloaded_video(file_id):
    path = find_video_file(file_id)
    if path is None:
        raise RuntimeError("Downloaded video file was not found.")
    target = UPLOAD_DIR / f"{file_id}.mp4"
    if path == target:
        return target
    if path.suffix.lower() == ".mp4":
        shutil.move(str(path), str(target))
        return target
    result = run_command([
        FFMPEG, "-y", "-i", str(path), "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(target)
    ])
    if result.returncode != 0:
        raise RuntimeError(f"Could not convert downloaded video: {result.stderr}")
    path.unlink(missing_ok=True)
    return target


# ============================================================
# HOSTED AI HELPERS (GOOGLE GEMINI)
# ============================================================

def gemini_available():
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def gemini_model_available():
    return gemini_available()


# Compatibility aliases so the rest of the V20 pipeline can keep its existing flow.
def ollama_available():
    return gemini_available()


def ollama_model_available():
    return gemini_model_available()


def _gemini_generate(prompt, images=None, timeout=150, temperature=0.0, max_output_tokens=300):
    """Call Gemini REST generateContent using the Render GEMINI_API_KEY."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured on the server.")

    parts = [{"text": str(prompt)}]
    for image_b64 in (images or []):
        if image_b64:
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": image_b64,
                }
            })

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": float(temperature),
            "maxOutputTokens": int(max_output_tokens),
        },
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="ignore")[:1200]
        except Exception:
            pass
        raise RuntimeError(f"Gemini API HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini API connection failed: {exc.reason}") from exc

    candidates = data.get("candidates") or []
    if not candidates:
        prompt_feedback = data.get("promptFeedback") or {}
        block_reason = prompt_feedback.get("blockReason")
        if block_reason:
            raise RuntimeError(f"Gemini blocked the request: {block_reason}")
        raise RuntimeError("Gemini returned no candidate response.")

    content = candidates[0].get("content") or {}
    response_parts = content.get("parts") or []
    answer = "\n".join(
        str(part.get("text"))
        for part in response_parts
        if isinstance(part, dict) and part.get("text") is not None
    ).strip()
    if not answer:
        raise RuntimeError("Gemini returned an empty response.")
    return answer


def extract_frame_base64(video_path: Path, timestamp: float):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        height, width = frame.shape[:2]
        if width > 768:
            new_width = 768
            new_height = max(1, int(height * new_width / width))
            frame = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), 72],
        )
        if not ok:
            return None
        return base64.b64encode(encoded.tobytes()).decode("ascii")
    finally:
        cap.release()


def transcript_for_window(cues, start, end, max_chars=1400):
    pieces = []
    for cue in cues:
        if cue["end"] <= start:
            continue
        if cue["start"] >= end:
            continue
        pieces.append(cue["text"])
        if sum(len(x) for x in pieces) >= max_chars:
            break
    text_value = " ".join(pieces).strip()
    return text_value[:max_chars]


def extract_json_object(text_value):
    text_value = (text_value or "").strip()
    text_value = re.sub(r"^```(?:json)?\s*", "", text_value, flags=re.I)
    text_value = re.sub(r"\s*```$", "", text_value)
    match = re.search(r"\{.*\}", text_value, flags=re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def ai_review_candidates(video_path, recommendations, cues, title, max_candidates=5):
    """Have Gemini compare several candidate moments in one multimodal request."""
    candidates = recommendations[:max_candidates]
    if not candidates:
        return {}, False, "No candidates available for AI review."
    if not gemini_available():
        return {}, False, "Gemini API is not configured."

    images = []
    candidate_notes = []
    for idx, candidate in enumerate(candidates, start=1):
        start_value = float(candidate["start"])
        end_value = float(candidate["end"])
        sample_time = start_value + max(0.1, end_value - start_value) * 0.50
        frame = extract_frame_base64(video_path, sample_time)
        if frame:
            images.append(frame)
            transcript = transcript_for_window(cues, start_value, end_value, max_chars=650)
            candidate_notes.append(
                f"Candidate {idx}: {start_value:.2f}-{end_value:.2f}s\n"
                f"Heuristic score: {candidate.get('score', 50)}\n"
                f"Transcript: {transcript!r}"
            )

    if not images:
        return {}, False, "Could not extract frames for AI comparison."

    prompt = f"""
You are the final highlight-ranking editor inside MAIGO.
The video title is: {title!r}

Compare the candidate moments below and rank them for short-form retention.
Candidate 1 in the text corresponds to image 1, Candidate 2 to image 2, and so on.
Do not invent events. Use only what the supplied frames and transcript support.
Prefer moments with a clear hook, visible action, surprise/payoff, strong reaction,
useful reveal, novelty, or a reason to keep watching. Penalize boring, unclear,
repetitive, or contextless moments.

{chr(10).join(candidate_notes)}

Return ONLY one JSON object in this exact shape:
{{
  "ranked": [
    {{"candidate_index": 1, "score": 0, "reason": "...", "hook": "...", "caption": "...", "hashtags": ["#tag"], "keep": true}}
  ]
}}

Include every candidate exactly once. Scores must be integers from 0 to 100.
Keep reason/hook/caption concise. Use 3-6 relevant hashtags per candidate.
""".strip()

    try:
        answer = _gemini_generate(prompt, images=images, timeout=150, temperature=0.15, max_output_tokens=500)
        parsed = extract_json_object(answer)
        ranked = parsed.get("ranked") if isinstance(parsed, dict) else None
        if not isinstance(ranked, list):
            raise RuntimeError("Gemini did not return a ranked candidate list.")

        updates = {}
        seen = set()
        for item in ranked:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("candidate_index"))
            except Exception:
                continue
            if idx < 1 or idx > len(candidates) or idx in seen:
                continue
            try:
                score = max(0, min(100, int(item.get("score", candidates[idx - 1].get("score", 50)))))
            except Exception:
                score = int(candidates[idx - 1].get("score", 50))
            hashtags = item.get("hashtags", [])
            if not isinstance(hashtags, list):
                hashtags = []
            keep_raw = item.get("keep", True)
            keep = keep_raw.strip().lower() not in {"false", "no", "0"} if isinstance(keep_raw, str) else bool(keep_raw)
            updates[idx] = {
                "score": score,
                "reason": str(item.get("reason") or "Gemini compared the visual and spoken signals."),
                "hook": str(item.get("hook") or "Wait for what happens next 👀"),
                "caption": str(item.get("caption") or "This moment deserves a short 👀"),
                "hashtags": [str(tag).strip() for tag in hashtags if str(tag).strip()][:6],
                "keep": keep,
            }
            seen.add(idx)

        if len(updates) < len(candidates):
            raise RuntimeError("Gemini ranking was incomplete.")

        return updates, True, f"Gemini compared {len(candidates)} candidate moments in one vision pass."
    except Exception as exc:
        print("[MAIGO] Gemini batch-ranking warning:", repr(exc))
        return {}, False, "Gemini comparison failed; using the fast fallback ranking."


def ai_review_single_candidate(video_path, candidate, cues, title):
    if not gemini_available():
        return None
    start_value = float(candidate["start"])
    end_value = float(candidate["end"])
    duration_value = max(0.1, end_value - start_value)
    frame = extract_frame_base64(video_path, start_value + duration_value * 0.50)
    if not frame:
        return None

    transcript = transcript_for_window(cues, start_value, end_value)
    prompt = f"""
You are the AI highlight editor inside MAIGO.
Analyze this candidate short-form clip from the video titled: {title!r}
Candidate timing: {start_value:.2f} to {end_value:.2f} seconds.
Transcript excerpt (may be empty): {transcript!r}
The attached image is a sampled frame from this exact candidate.
Judge whether this moment is interesting for TikTok, Reels, or YouTube Shorts.
Do NOT invent events that are not supported by the frame or transcript.
Return ONLY JSON with keys:
score (integer 0-100), reason, hook, caption, hashtags (array of 3-6), keep (boolean).
""".strip()

    try:
        answer = _gemini_generate(prompt, images=[frame], timeout=120, temperature=0.2, max_output_tokens=260)
        parsed = extract_json_object(answer)
        if not isinstance(parsed, dict):
            return None
        try:
            score = max(0, min(100, int(parsed.get("score", candidate.get("score", 50)))))
        except Exception:
            score = int(candidate.get("score", 50))
        hashtags = parsed.get("hashtags", [])
        if not isinstance(hashtags, list):
            hashtags = []
        keep_raw = parsed.get("keep", True)
        keep = keep_raw.strip().lower() not in {"false", "no", "0"} if isinstance(keep_raw, str) else bool(keep_raw)
        return {
            "score": score,
            "reason": str(parsed.get("reason", "Gemini reviewed the visual and spoken signals.")),
            "hook": str(parsed.get("hook", "Wait for what happens next 👀")),
            "caption": str(parsed.get("caption", "This moment deserves a short 👀")),
            "hashtags": [str(tag).strip() for tag in hashtags if str(tag).strip()][:6],
            "keep": keep,
        }
    except Exception as exc:
        print("[MAIGO] Gemini single-candidate warning:", repr(exc))
        return None


@app.get("/")
def root():
    # Serve the public MAIGO web interface from the same Render service.
    frontend = BASE_DIR.parent / "frontend" / "index.html"
    if frontend.exists():
        return FileResponse(frontend, media_type="text/html")
    return {"app": "MAIGO", "status": "online", "version": "20.1.0", "ai_provider": "Google Gemini", "ai_model": AI_MODEL}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/status")
def api_status():
    return {"app": "MAIGO", "status": "online", "version": "20.1.0", "ai_provider": "Google Gemini", "ai_model": AI_MODEL}


@app.get("/api/ai-status")
def ai_status():
    configured = gemini_available()
    return {
        "success": True,
        "ai_provider": "Google Gemini",
        "gemini_configured": configured,
        "ollama_running": configured,
        "model": AI_MODEL,
        "model_ready": configured,
        "message": "Gemini AI is ready." if configured else "GEMINI_API_KEY is not configured on the server.",
    }



@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)):
    file_id = str(uuid.uuid4())
    temp = UPLOAD_DIR / f"{file_id}_temp"
    target = UPLOAD_DIR / f"{file_id}.mp4"
    try:
        with open(temp, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        result = run_command([FFMPEG, "-y", "-i", str(temp), "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(target)])
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        info = get_video_info(target)
        title = file.filename or "Uploaded video"
        save_metadata(file_id, title)
        return {"success": True, "file_id": file_id, "title": title, "video": info}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        temp.unlink(missing_ok=True)


@app.post("/api/analyze-url")
def analyze_url(request: AnalyzeRequest):
    url = request.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required.")
    try:
        check_tools()
        file_id = str(uuid.uuid4())
        template = str(UPLOAD_DIR / f"{file_id}.%(ext)s")
        common = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "force_ipv4": True,
            "extractor_args": {"youtube": {"player_client": ["android"]}},
        }
        with yt_dlp.YoutubeDL(common) as ydl:
            info = ydl.extract_info(url, download=False)
        title = info.get("title", "Downloaded video")
        language = choose_subtitle_language(info)
        options = {
            **common,
            "outtmpl": template,
            "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
            "retries": 3,
            "fragment_retries": 3,
            "concurrent_fragment_downloads": 1,
            "cachedir": False,
            "ffmpeg_location": str(Path(FFMPEG).parent),
        }
        if language:
            options.update({
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [language],
                "subtitlesformat": "vtt/best",
            })
        print("[MAIGO] Downloading:", title)
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(url, download=True)
        video_path = normalize_downloaded_video(file_id)
        subtitle = find_subtitle_file(file_id)
        save_metadata(file_id, title, url, language if subtitle else "")
        info = get_video_info(video_path)
        print("[MAIGO] Download complete.")
        return {
            "success": True,
            "source_url": url,
            "file_id": file_id,
            "title": title,
            "video": info,
            "transcript_available": subtitle is not None,
            "transcript_language": language if subtitle else "",
        }
    except Exception as exc:
        print("[MAIGO] ANALYZE ERROR:", repr(exc))
        raise HTTPException(status_code=500, detail=f"Analyze failed: {exc}") from exc



@app.get("/api/export-presets")
def export_presets():
    return {
        "success": True,
        "presets": {
            "fast": {"name": "Fast", "resolution": "720", "quality": "fast", "description": "Fastest render for testing"},
            "balanced": {"name": "Balanced", "resolution": "1080", "quality": "balanced", "description": "Recommended quality/speed"},
            "high": {"name": "High", "resolution": "1080", "quality": "high", "description": "Higher quality, slower render"},
        },
    }

@app.get("/api/caption-presets")
def caption_presets():
    return {
        "success": True,
        "presets": {
            "wordpop": {
                "name": "Word Pop",
                "font": "Arial Black",
                "size": 54,
                "color": "#ffffff",
                "accent": "#00ffff",
                "weight": 900,
                "shadow": "0 4px 0 #111",
                "description": "Animated word emphasis",
            },
            "classic": {
                "name": "Classic",
                "font": "Arial",
                "size": 52,
                "color": "#ffffff",
                "accent": "#ffffff",
                "weight": 700,
                "shadow": "0 3px 0 #111",
                "description": "Clean creator captions",
            },
            "punchy": {
                "name": "Punchy",
                "font": "Impact",
                "size": 58,
                "color": "#ffffff",
                "accent": "#00ffff",
                "weight": 900,
                "shadow": "0 5px 0 #000",
                "description": "Big, bold, high impact",
            },
            "clean": {
                "name": "Clean",
                "font": "Trebuchet MS",
                "size": 50,
                "color": "#ffffff",
                "accent": "#00ffff",
                "weight": 700,
                "shadow": "0 3px 0 #151515",
                "description": "Soft modern style",
            },
            "neon": {
                "name": "Neon",
                "font": "Arial Black",
                "size": 52,
                "color": "#ffffff",
                "accent": "#00ffff",
                "weight": 900,
                "shadow": "0 0 10px #ff00ff",
                "description": "Bright gaming style",
            },
            "meme": {
                "name": "Meme",
                "font": "Impact",
                "size": 60,
                "color": "#ffffff",
                "accent": "#00ffff",
                "weight": 900,
                "shadow": "4px 4px 0 #000",
                "description": "Loud meme captions",
            },
            "typewriter": {
                "name": "Typewriter",
                "font": "Courier New",
                "size": 44,
                "color": "#ffffff",
                "accent": "#ffffff",
                "weight": 400,
                "shadow": "0 2px 0 #111",
                "description": "Retro terminal feel",
            },
            "minimal": {
                "name": "Minimal",
                "font": "Arial",
                "size": 46,
                "color": "#ffffff",
                "accent": "#ffffff",
                "weight": 400,
                "shadow": "0 2px 0 #000",
                "description": "Low-key and clean",
            },
            "off": {
                "name": "Off",
                "font": "Arial",
                "size": 46,
                "color": "#ffffff",
                "accent": "#ffffff",
                "weight": 400,
                "shadow": "none",
                "description": "No text burned in",
            },
        },
    }

@app.post("/api/recommendations")
def recommendations(request: RecommendationRequest):
    try:
        print("[MAIGO] Recommendation request:", request.file_id)
        video = find_video_file(request.file_id)
        if video is None:
            raise RuntimeError("Video file not found.")
        info = get_video_info(video)
        meta = load_metadata(request.file_id)
        subtitle = find_subtitle_file(request.file_id)
        cues = parse_subtitles(subtitle) if subtitle else []
        print("[MAIGO] Detecting visual changes...")
        try:
            scenes = detect_scene_changes(video)
        except Exception as exc:
            print("[MAIGO] Scene warning:", repr(exc))
            scenes = []
        results = create_recommendations(info["duration_seconds"], scenes, cues, meta.get("title", "Video"))
        ai_results, ai_used, ai_message = apply_ai_brain(
            video,
            results,
            cues,
            meta.get("title", "Video"),
        )
        print("[MAIGO] AI status:", ai_message)
        print("[MAIGO] Recommendations complete.")
        return {
            "success": True,
            "file_id": request.file_id,
            "title": meta.get("title", "Video"),
            "video": info,
            "scene_changes_found": len(scenes),
            "transcript_available": bool(cues),
            "transcript_cues_found": len(cues),
            "ai_enabled": ai_used,
            "ai_model": AI_MODEL,
            "ai_message": ai_message,
            "recommendations": ai_results,
        }
    except Exception as exc:
        print("[MAIGO] RECOMMENDATION ERROR:", repr(exc))
        raise HTTPException(status_code=500, detail=f"Recommendation engine error: {exc}") from exc



def _chat_default_range(recommendations):
    if recommendations:
        best = recommendations[0]
        return float(best.get("start", 0)), float(best.get("end", 20)), best
    return 0.0, 20.0, {}


def _chat_extract_json(text):
    parsed = extract_json_object(text)
    if isinstance(parsed, dict):
        return parsed
    return None


def _chat_fallback_plan(message, recommendations):
    """Cheap local fallback for common editing commands if Qwen returns malformed JSON."""
    text = (message or "").strip().lower()
    start, end, best = _chat_default_range(recommendations)

    if not text:
        return {"action": "answer", "reply": "Tell me what you want MAIGO to make."}

    # Questions asking how to use MAIGO should stay explanatory.
    if text.startswith(("how do i", "how to", "what do i", "where do i")) and not re.search(r"\b(?:clip|cut|trim|make)\s+(?:the|a|this)\b", text):
        return {"action": "answer", "reply": "Tell me what part you want clipped and I can find it. Examples: “clip the part where he kills the cows”, “make clip #3”, or “make a 15-second clip from 1:00 to 1:15”."}

    # Explicit time range: e.g. 1:00 to 1:20 or 60-80 seconds.
    m = re.search(r"(?:(\d+):)?(\d{1,3}(?:\.\d+)?)\s*(?:to|-|–)\s*(?:(\d+):)?(\d{1,3}(?:\.\d+)?)\s*(?:seconds?|secs?)?", text)
    if m and ("clip" in text or "short" in text or "cut" in text):
        def tc(mm, ss):
            return float(mm or 0) * 60.0 + float(ss)
        s = tc(m.group(1), m.group(2))
        e = tc(m.group(3), m.group(4))
        return {"action": "create_clip", "start": s, "end": e, "reply": f"Creating your clip from {s:.1f}s to {e:.1f}s."}

    dur_match = re.search(r"(?:make|create|cut|give me)\s+(?:a\s+)?(?:clip|short).*?(\d+)\s*(?:second|sec|s)", text)
    if dur_match:
        dur = max(5, min(60, int(dur_match.group(1))))
        center = (start + end) / 2.0
        s = max(0.0, center - dur / 2.0)
        return {"action": "create_clip", "start": s, "end": s + dur, "reply": f"Creating a {dur}-second clip around MAIGO's strongest moment."}

    num = re.search(r"(?:clip|short)\s*#?\s*(\d+)", text)
    chosen = best
    if num:
        idx = max(1, int(num.group(1)))
        if 1 <= idx <= len(recommendations):
            chosen = recommendations[idx - 1]
            start = float(chosen.get("start", start))
            end = float(chosen.get("end", end))

    # Natural-language content descriptions must use the semantic video search path.
    descriptive_markers = (
        "where he ", "where she ", "where they ", "where the ", "part where", "moment where",
        "scene where", "when he ", "when she ", "when they ", "after he ", "before he ",
        "after she ", "before she ", "kills ", "killing ", "climbing ", "climbs ", "cuts to",
        "shows him", "shows her", "shows them", "the part he", "the part she", "the moment he",
        "the moment she", "find the part", "find where", "find the moment"
    )
    if any(marker in text for marker in descriptive_markers):
        return {
            "action": "find_and_clip",
            "description": message.strip(),
            "reply": "I’ll search the video for that exact moment and clip it."
        }

    if "best short" in text or "best clip as a short" in text:
        return {"action": "create_best_short", "reply": "Creating the AI-ranked #1 moment as a 9:16 Short."}
    if "short" in text or "vertical" in text or "9:16" in text:
        return {"action": "create_short", "candidate": int(num.group(1)) if num else 1, "start": start, "end": end, "reply": "Creating the 9:16 Short from the selected moment."}
    if "clip" in text or "cut" in text or "trim" in text or ("make" in text and recommendations):
        return {"action": "create_clip", "candidate": int(num.group(1)) if num else 1, "start": start, "end": end, "reply": "Creating the clip from MAIGO's selected moment."}
    return {"action": "answer", "reply": "Tell me what part of the video to clip, or give me a time range. Example: “Clip the part where he kills the cows and then climbs the tree.”"}


def _chat_is_edit_request(text):
    t = (text or "").strip().lower()
    if t.startswith(("how do i", "how to", "what do i", "where do i")) and not any(x in t for x in ("clip the", "cut the", "make the", "find the")):
        return False
    return bool(re.search(r"\b(clip|cut|trim|make|create|edit|export|find)\b", t))


def _chat_format_time(seconds):
    seconds = max(0, int(float(seconds or 0)))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _chat_search_terms(text):
    """Extract useful search concepts while preserving action words that matter to video search."""
    stop = {
        "clip", "the", "part", "where", "he", "she", "they", "him", "her", "them", "that", "this",
        "and", "then", "to", "of", "a", "an", "on", "in", "from", "for", "with", "me", "please",
        "make", "create", "find", "cut", "trim", "show", "scene", "moment", "video", "one",
        "after", "before", "when", "does", "do", "did", "it", "is", "are", "was", "were", "his", "their",
        "cuts", "cut", "part", "give", "want", "need", "can", "you", "my", "out", "get", "into", "over"
    }
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    return [w for w in words if len(w) >= 3 and w not in stop]


def _chat_semantic_concepts(text):
    """Expand common video descriptions into concrete visual concepts."""
    low = (text or "").lower()
    concepts = []
    expansions = {
        "cow": ["cow", "cows", "animal", "livestock"],
        "cows": ["cow", "cows", "animal", "livestock"],
        "kill": ["kill", "killing", "hit", "attack", "death"],
        "kills": ["kill", "killing", "hit", "attack", "death"],
        "killing": ["kill", "killing", "hit", "attack", "death"],
        "tree": ["tree", "leaves", "wood", "trunk"],
        "climb": ["climb", "climbing", "ladder", "up", "tree"],
        "climbing": ["climb", "climbing", "ladder", "up", "tree"],
        "climbs": ["climb", "climbing", "ladder", "up", "tree"],
        "house": ["house", "building", "home"],
        "jump": ["jump", "fall", "air"],
        "fight": ["fight", "attack", "combat", "hit"],
    }
    for key, vals in expansions.items():
        if re.search(rf"\b{re.escape(key)}\b", low):
            concepts.extend(vals)
    concepts.extend(_chat_search_terms(text))
    return list(dict.fromkeys(concepts))


def _chat_candidate_windows(duration, recommendations, scenes, cues, message, max_windows=12):
    """Build a broad but compact temporal search grid.

    Unlike V19, this intentionally covers the whole video even when transcript/scene signals are weak.
    """
    candidates = []

    def add_window(start, end, source, match_score=0, label=""):
        start = max(0.0, float(start))
        end = min(float(duration), float(end))
        if end <= start + 1.0:
            return
        midpoint = (start + end) / 2.0
        for x in candidates:
            old_mid = (x["start"] + x["end"]) / 2.0
            if abs(midpoint - old_mid) < 5.0:
                if match_score > x.get("match_score", 0):
                    x.update({"start": round(start, 2), "end": round(end, 2), "source": source, "match_score": int(match_score), "label": label})
                return
        candidates.append({"start": round(start, 2), "end": round(end, 2), "source": source, "match_score": int(match_score), "label": label})

    terms = _chat_semantic_concepts(message)
    if cues and terms:
        scored = []
        for cue in cues:
            text = cue.get("text", "")
            cue_words = set(re.findall(r"[a-z0-9']+", text.lower()))
            overlap = 0
            for term in terms:
                if term in cue_words or any(term in w or w in term for w in cue_words if len(w) >= 3):
                    overlap += 1
            phrase_bonus = sum(2 for term in terms if term and term in text.lower())
            if overlap or phrase_bonus:
                scored.append((overlap * 5 + phrase_bonus, cue))
        for score, cue in sorted(scored, key=lambda x: x[0], reverse=True)[:6]:
            add_window(cue["start"] - 7.0, cue["start"] + 18.0, "transcript", score, "transcript hit")

    # Keep prior recommendations in the search set, but do not let them crowd out video coverage.
    for item in recommendations[:5]:
        add_window(item.get("start", 0), item.get("end", 20), "recommendation", item.get("score", 0) // 10, "AI recommendation")

    # Every segment gets represented. 25s windows overlap, allowing two sequential events to land together.
    window = 25.0 if duration > 60 else max(12.0, min(25.0, duration))
    stride = max(8.0, window * 0.55)
    pos = 0.0
    while pos < duration:
        add_window(pos, min(duration, pos + window), "coverage", 0, "full-video coverage")
        if len(candidates) >= max_windows * 2:
            break
        pos += stride

    # Scene changes can refine the grid without becoming the only signal.
    for scene in scenes[:30]:
        add_window(scene - 8.0, scene + 17.0, "scene", 1, "scene change")

    # Sort by search signal, but retain coverage diversity.
    candidates.sort(key=lambda x: (x.get("match_score", 0), x["source"] == "transcript", x["source"] == "recommendation"), reverse=True)
    selected = []
    for c in candidates:
        midpoint = (c["start"] + c["end"]) / 2.0
        if any(abs(midpoint - ((x["start"] + x["end"]) / 2.0)) < 9.0 for x in selected):
            continue
        selected.append(c)
        if len(selected) >= max_windows:
            break
    return selected


def _chat_make_contact_sheet(video_path, candidates, columns=4):
    """Create one labeled image so the small hosted Gemini vision model receives a single multimodal input."""
    thumbs = []
    for idx, c in enumerate(candidates, start=1):
        start, end = float(c["start"]), float(c["end"])
        ts = start + (end - start) * 0.52
        frame_b64 = extract_frame_base64(video_path, ts)
        if not frame_b64:
            continue
        try:
            import numpy as np
            raw = base64.b64decode(frame_b64)
            arr = np.frombuffer(raw, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                continue
            frame = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
            cv2.rectangle(frame, (0, 0), (320, 34), (0, 0, 0), -1)
            label = f"#{idx}  {_chat_format_time(start)}-{_chat_format_time(end)}"
            cv2.putText(frame, label, (8, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255,255,255), 1, cv2.LINE_AA)
            thumbs.append(frame)
        except Exception:
            continue

    if not thumbs:
        return None
    rows = (len(thumbs) + columns - 1) // columns
    sheet = np.zeros((rows * 180, columns * 320, 3), dtype=np.uint8)
    for i, frame in enumerate(thumbs):
        y = (i // columns) * 180
        x = (i % columns) * 320
        sheet[y:y+180, x:x+320] = frame
    ok, encoded = cv2.imencode(".jpg", sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
    if not ok:
        return None
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def _chat_semantic_search(video_path, message, recommendations, title):
    """Find a described moment using whole-video coverage + one compact contact sheet + focused confirmation."""
    info = get_video_info(video_path)
    duration = info["duration_seconds"]
    sid = str(Path(video_path).stem)
    subtitle = find_subtitle_file(sid)
    cues = parse_subtitles(subtitle) if subtitle else []
    try:
        scenes = detect_scene_changes(video_path)
    except Exception as exc:
        print("[MAIGO] Semantic scene detection warning:", repr(exc))
        scenes = []

    candidates = _chat_candidate_windows(duration, recommendations, scenes, cues, message, max_windows=12)
    if not candidates:
        return None, False, "I could not build a search set for that request."
    if not ollama_available() or not ollama_model_available():
        chosen = max(candidates, key=lambda x: x.get("match_score", 0))
        chosen["confidence"] = 0
        chosen["reason"] = "Gemini AI is unavailable; no visual search was performed."
        return chosen, False, "Gemini AI is unavailable."

    sheet = _chat_make_contact_sheet(video_path, candidates)
    if not sheet:
        return None, False, "MAIGO could not create the visual search sheet."

    # Add transcript snippets to help the model distinguish sequences that are visually similar.
    notes = []
    for idx, c in enumerate(candidates, start=1):
        excerpt = ""
        if cues:
            excerpt = transcript_for_window(cues, float(c["start"]), float(c["end"]), max_chars=260)
        notes.append(f"#{idx} {c['start']:.2f}-{c['end']:.2f}s: {excerpt!r}")

    prompt = f"""
You are MAIGO's video finder. You MUST choose the numbered time window that best matches the user's request.
User request: {message!r}
Video: {title!r}

The image is a contact sheet. Each tile has its candidate number and time range.
Choose the best tile using visual evidence. Transcript snippets are supporting evidence.
The requested actions may happen in sequence, for example: FIRST kill cows, THEN the video cuts to climbing a tree.
Prefer a window that can contain both events, in that order.
Do not explain your limitations. Do not say you cannot edit videos.

Candidate transcript snippets:
{chr(10).join(notes)}

Return ONLY a JSON object, no markdown:
{{"candidate_index": 1, "confidence": 0, "reason": "brief evidence"}}
""".strip()

    try:
        answer = _gemini_generate(prompt, images=[sheet], timeout=150, temperature=0.0, max_output_tokens=180)
        parsed = extract_json_object(answer)
        if not isinstance(parsed, dict):
            # Tiny local models sometimes return a perfectly useful sentence instead of strict JSON.
            # Recover a candidate number rather than discarding the visual search.
            m_idx = re.search(r"(?:candidate|option|tile)\s*#?\s*(\d{1,2})", answer or "", flags=re.I)
            if m_idx:
                parsed = {"candidate_index": int(m_idx.group(1)), "confidence": 35, "reason": (answer or "")[:220]}
            else:
                raise RuntimeError(f"Model returned non-JSON output: {answer[:180]!r}")
        idx = int(parsed.get("candidate_index", 1) or 1)
        idx = max(1, min(len(candidates), idx))
        chosen = dict(candidates[idx - 1])
        chosen["confidence"] = max(0, min(100, int(parsed.get("confidence", 0) or 0)))
        chosen["reason"] = str(parsed.get("reason") or "Visual search matched the requested moment.")

        # Focused confirmation: inspect 3 frames from the chosen window in a second compact sheet.
        confirm_candidates = [chosen]
        confirm_sheet = _chat_make_contact_sheet(video_path, [
            {"start": chosen["start"], "end": chosen["start"] + (chosen["end"] - chosen["start"]) * 0.45},
            {"start": chosen["start"] + (chosen["end"] - chosen["start"]) * 0.40, "end": chosen["start"] + (chosen["end"] - chosen["start"]) * 0.78},
            {"start": chosen["start"] + (chosen["end"] - chosen["start"]) * 0.70, "end": chosen["end"]},
        ], columns=3)
        if confirm_sheet:
            confirm_prompt = f"""
Check this 3-frame sequence from {chosen['start']:.2f}-{chosen['end']:.2f}s against the user's request: {message!r}
Answer ONLY JSON: {{"match": true, "confidence": 0-100, "reason": "brief evidence"}}
Do not invent events. If the sequence does not show enough evidence, use match=false.
""".strip()
            try:
                ans2 = _gemini_generate(confirm_prompt, images=[confirm_sheet], timeout=120, temperature=0.0, max_output_tokens=140)
            except Exception as confirm_exc:
                print("[MAIGO] Gemini confirmation warning:", repr(confirm_exc))
                ans2 = ""
            parsed2 = extract_json_object(ans2)
            if not isinstance(parsed2, dict):
                parsed2 = {}
                low2 = (ans2 or "").lower()
                if any(x in low2 for x in ("does match", "matches", "yes", "true")):
                    parsed2 = {"match": True, "confidence": 45, "reason": (ans2 or "")[:220]}
                elif any(x in low2 for x in ("does not match", "not match", "no", "false")):
                    parsed2 = {"match": False, "confidence": 20, "reason": (ans2 or "")[:220]}
            if isinstance(parsed2, dict) and parsed2:
                if not bool(parsed2.get("match", False)) and chosen.get("confidence", 0) < 65:
                    # Do not blindly reject; instead mark low confidence so the UI remains honest.
                    chosen["confidence"] = min(chosen.get("confidence", 0), 50)
                elif parsed2.get("confidence") is not None:
                    chosen["confidence"] = max(chosen.get("confidence", 0), min(100, int(parsed2.get("confidence", 0) or 0)))
                chosen["reason"] = str(parsed2.get("reason") or chosen["reason"])

        return chosen, True, f"AI visually searched {len(candidates)} whole-video windows and confirmed the closest match."
    except Exception as exc:
        print("[MAIGO] Semantic search warning:", repr(exc))
        # Still return the strongest candidate, but say clearly that AI visual search failed.
        chosen = max(candidates, key=lambda x: x.get("match_score", 0))
        chosen["confidence"] = 0
        chosen["reason"] = "AI visual search could not complete; this is only the strongest non-AI candidate."
        return chosen, False, "AI visual search failed before a reliable visual match was found."

def _chat_ai_plan(message, recommendations, settings, title):
    context_lines = []
    for i, item in enumerate(recommendations[:5], start=1):
        context_lines.append(
            f"Candidate {i}: {float(item.get('start', 0)):.2f}-{float(item.get('end', 0)):.2f}s; "
            f"score {item.get('score', 0)}; hook={item.get('hook', '')!r}; caption={item.get('caption', '')!r}"
        )
    context = "\n".join(context_lines) if context_lines else "No AI shortlist is available yet."
    settings_text = json.dumps(settings or {}, ensure_ascii=False)

    prompt = f"""
You are MAIGO's editing command planner. You control a local video editor.
Never answer with a generic refusal such as “I cannot create video clips.”
For an editing request, you MUST return an executable action as JSON.
Video title: {title!r}

Available recommended moments:
{context}

Current editor settings:
{settings_text}

User request:
{message!r}

Allowed actions:
- create_clip: create a normal MP4 clip from start/end seconds
- create_short: create a 9:16 Short from start/end seconds using current settings
- create_best_short: create the #1 recommended Short
- answer: explain/help without changing the video

Rules:
- “make a clip” with no explicit timing -> candidate 1.
- “make clip #3” -> candidate 3.
- Requested duration -> center it around the selected candidate midpoint.
- Explicit times -> obey them.
- “Short”, “vertical”, “9:16” -> create_short.
- “best short” -> create_best_short.
- A request describing a moment by actions/events (for example “clip the part where he kills the cows and then climbs the tree”) -> DO NOT answer; return action “find_and_clip”.
- If the request begins with “how do I” and is genuinely a tutorial question, use answer.

Return ONLY JSON with exactly:
{{
  "action": "create_clip|create_short|create_best_short|find_and_clip|answer",
  "candidate": 1,
  "start": 0,
  "end": 20,
  "duration": 20,
  "description": "",
  "reply": "short response"
}}
""".strip()

    answer = _gemini_generate(prompt, images=None, timeout=90, temperature=0.0, max_output_tokens=320)
    parsed = _chat_extract_json(answer)
    if not isinstance(parsed, dict):
        raise RuntimeError("AI assistant did not return a valid plan.")
    return parsed


@app.post("/api/ai-chat")
def ai_chat(request: AIChatRequest):
    """Natural-language MAIGO command bar with semantic video search for descriptive requests."""
    message = (request.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Type a request first.")

    file_id = (request.file_id or "").strip()
    if not file_id:
        return {"success": True, "executed": False, "action": "answer", "reply": "Analyze a video first, then tell me what you want MAIGO to make."}

    video = find_video_file(file_id)
    if video is None:
        raise HTTPException(status_code=400, detail="The current video could not be found. Analyze it again.")

    meta = load_metadata(file_id)
    recommendations = request.recommendations or []
    settings = request.settings or {}
    text_lower = message.lower()

    # Descriptive requests bypass the generic chatbot path. This is the important fix:
    # MAIGO searches the actual video instead of allowing the LLM to produce a refusal.
    descriptive = any(marker in text_lower for marker in (
        "where he ", "where she ", "where they ", "part where", "moment where", "scene where",
        "when he ", "when she ", "after he ", "before he ", "after she ", "before she ",
        "kills ", "killing ", "climbing ", "climbs ", "cuts to", "shows him", "shows her",
        "find the part", "find where", "find the moment"
    )) and _chat_is_edit_request(message)

    try:
        if descriptive:
            chosen, ai_used, search_message = _chat_semantic_search(video, message, recommendations, meta.get("title", "Video"))
            if not chosen:
                raise RuntimeError(search_message)
            duration_total = get_video_info(video)["duration_seconds"]
            start = max(0.0, min(float(chosen["start"]), max(0.0, duration_total - 0.1)))
            end = max(start + 0.1, min(float(chosen["end"]), duration_total))
            clip_data = create_clip(ClipRequest(file_id=file_id, start=start, end=end))
            confidence = int(chosen.get("confidence", 0) or 0)
            reason = str(chosen.get("reason", ""))
            confidence_text = f" Confidence {confidence}/100." if confidence else ""
            reply = f"I found the closest match at {_chat_format_time(start)}–{_chat_format_time(end)} and clipped it.{confidence_text}"
            if reason:
                reply += f" {reason}"
            return {"success": True, "executed": True, "action": "create_clip", "reply": reply, "ai_used": ai_used, "search_message": search_message, "result": clip_data}

        plan = None
        ai_used = False
        try:
            if ollama_available() and ollama_model_available():
                plan = _chat_ai_plan(message, recommendations, settings, meta.get("title", "Video"))
                ai_used = True
        except Exception as exc:
            print("[MAIGO] AI chat planning warning:", repr(exc))

        # Protect against the exact failure the user saw: if Qwen says “I cannot create…”,
        # treat that as a bad plan and use MAIGO's deterministic executor instead.
        if not isinstance(plan, dict) or (_chat_is_edit_request(message) and str(plan.get("action", "answer")).lower() == "answer"):
            plan = _chat_fallback_plan(message, recommendations)

        action = str(plan.get("action", "answer")).strip().lower()
        reply = str(plan.get("reply") or "Okay.").strip()

        if action == "find_and_clip":
            chosen, search_ai_used, search_message = _chat_semantic_search(video, message, recommendations, meta.get("title", "Video"))
            if not chosen:
                raise RuntimeError(search_message)
            duration_total = get_video_info(video)["duration_seconds"]
            start = max(0.0, min(float(chosen["start"]), max(0.0, duration_total - 0.1)))
            end = max(start + 0.1, min(float(chosen["end"]), duration_total))
            clip_data = create_clip(ClipRequest(file_id=file_id, start=start, end=end))
            confidence = int(chosen.get("confidence", 0) or 0)
            reason = str(chosen.get("reason", ""))
            reply = f"Found and clipped the requested moment at {_chat_format_time(start)}–{_chat_format_time(end)}."
            if confidence:
                reply += f" Match confidence: {confidence}/100."
            if reason:
                reply += f" {reason}"
            return {"success": True, "executed": True, "action": "create_clip", "reply": reply, "ai_used": ai_used or search_ai_used, "search_message": search_message, "result": clip_data}

        if action == "create_best_short":
            if not recommendations:
                raise RuntimeError("Run Generate Recommendations first so MAIGO knows the #1 moment.")
            best = recommendations[0]
            mode = str(settings.get("attention_mode") or "none")
            attention_id = settings.get("attention_video_id") if mode != "none" else None
            if mode != "none" and not attention_id:
                raise RuntimeError(f"Upload your {mode} gameplay first.")
            render_request = BestShortRequest(
                file_id=file_id,
                start=float(best.get("start", 0)),
                end=float(best.get("end", 20)),
                score=int(best.get("score", 0) or 0),
                hook=str(best.get("hook", "")),
                caption=str(best.get("caption", "")),
                hashtags=list(best.get("hashtags", []) or []),
                attention_video_id=attention_id,
                attention_mode=mode,
                smart_framing=str(settings.get("smart_framing", "on")) == "on",
                caption_style=str(settings.get("caption_style", "wordpop")),
                caption_position=str(settings.get("caption_position", "bottom")),
                caption_size=str(settings.get("caption_size", "medium")),
                hook_overlay=str(settings.get("hook_overlay", "on")) == "on",
                resolution="720" if str(settings.get("export_quality", "balanced")) == "fast" else "1080",
                quality=str(settings.get("export_quality", "balanced")),
                background_blur=str(settings.get("background_blur", "on")) == "on",
                blur_strength=int(settings.get("blur_strength", 18) or 18),
            )
            result = create_best_short(render_request)
            return {"success": True, "executed": True, "action": action, "reply": reply or "Best Short created.", "ai_used": ai_used, "result": result}

        if action in {"create_clip", "create_short"}:
            if not recommendations and ("start" not in plan or "end" not in plan):
                raise RuntimeError("Run Generate Recommendations first, or tell me an exact time range.")

            candidate_number = int(plan.get("candidate", 1) or 1)
            if recommendations and 1 <= candidate_number <= len(recommendations):
                chosen = recommendations[candidate_number - 1]
            else:
                chosen = recommendations[0] if recommendations else {}

            default_start = float(chosen.get("start", 0))
            default_end = float(chosen.get("end", 20))
            start_value = plan.get("start")
            end_value = plan.get("end")
            start = float(start_value if start_value is not None else default_start)
            end = float(end_value if end_value is not None else default_end)

            duration = plan.get("duration")
            if duration is not None and float(duration) > 0:
                d = max(5.0, min(60.0, float(duration)))
                center = (default_start + default_end) / 2.0
                start = max(0.0, center - d / 2.0)
                end = start + d

            duration_total = get_video_info(video)["duration_seconds"]
            start = max(0.0, min(start, max(0.0, duration_total - 0.1)))
            end = max(start + 0.1, min(end, duration_total))

            if action == "create_clip":
                clip_data = create_clip(ClipRequest(file_id=file_id, start=start, end=end))
                return {"success": True, "executed": True, "action": action, "reply": reply or "Clip created.", "ai_used": ai_used, "result": clip_data}

            mode = str(settings.get("attention_mode") or "none")
            attention_id = settings.get("attention_video_id") if mode != "none" else None
            if mode != "none" and not attention_id:
                raise RuntimeError(f"Upload your {mode} gameplay first.")
            short_request = ShortRequest(
                file_id=file_id,
                start=start,
                end=end,
                attention_video_id=attention_id,
                attention_mode=mode,
                smart_framing=str(settings.get("smart_framing", "on")) == "on",
                caption_style=str(settings.get("caption_style", "wordpop")),
                caption_position=str(settings.get("caption_position", "bottom")),
                caption_size=str(settings.get("caption_size", "medium")),
                hook_overlay=str(settings.get("hook_overlay", "on")) == "on",
                hook_text=str(chosen.get("hook", "")),
                resolution="720" if str(settings.get("export_quality", "balanced")) == "fast" else "1080",
                quality=str(settings.get("export_quality", "balanced")),
                background_blur=str(settings.get("background_blur", "on")) == "on",
                blur_strength=int(settings.get("blur_strength", 18) or 18),
            )
            short_data = create_short(short_request)
            return {"success": True, "executed": True, "action": action, "reply": reply or "9:16 Short created.", "ai_used": ai_used, "result": {"short": short_data, "recommendation": chosen}}

        return {"success": True, "executed": False, "action": "answer", "reply": reply, "ai_used": ai_used}

    except HTTPException:
        raise
    except Exception as exc:
        print("[MAIGO] AI chat execute error:", repr(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/create-clip")
def create_clip(request: ClipRequest):
    try:
        video = find_video_file(request.file_id)
        if video is None:
            raise RuntimeError("Source video not found.")
        if request.start < 0 or request.end <= request.start:
            raise RuntimeError("Invalid clip range.")
        duration = get_video_info(video)["duration_seconds"]
        if request.start >= duration:
            raise RuntimeError("Start time is beyond the video duration.")
        end = min(request.end, duration)
        clip_id = str(uuid.uuid4())
        output = CLIP_DIR / f"{clip_id}.mp4"
        result = run_command([
            FFMPEG, "-y", "-ss", str(request.start), "-i", str(video),
            "-t", str(end - request.start), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-c:a", "aac", "-movflags", "+faststart", str(output)
        ])
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        return {
            "success": True,
            "clip_id": clip_id,
            "start": request.start,
            "end": end,
            "duration": round(end - request.start, 2),
            "message": "Clip created successfully.",
            "file": f"/clips/{output.name}",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc



@app.post("/api/create-best-short")
def create_best_short(request: BestShortRequest):
    """One-click renderer: use the already AI-ranked #1 moment supplied by the frontend."""
    try:
        print("[MAIGO] One-click Best Short request:", request.file_id, request.start, request.end)

        # V15 intentionally does NOT run the AI again.
        # The Suggestions panel already ranked the moments, so this endpoint
        # simply renders the selected #1 range. This saves another Gemini pass.
        if request.start < 0 or request.end <= request.start:
            raise RuntimeError("Invalid Best Short range.")

        best = {
            "rank": 1,
            "start": float(request.start),
            "end": float(request.end),
            "duration": round(float(request.end) - float(request.start), 2),
            "score": request.score if request.score is not None else 0,
            "hook": request.hook,
            "caption": request.caption,
            "ai_selected": True,
        }

        render_request = ShortRequest(
            file_id=request.file_id,
            start=float(request.start),
            end=float(request.end),
            attention_video_id=request.attention_video_id,
            attention_mode=request.attention_mode,
            smart_framing=request.smart_framing,
            caption_style=request.caption_style,
            caption_position=request.caption_position,
            caption_size=request.caption_size,
            hook_overlay=request.hook_overlay,
            hook_text=request.hook,
            resolution=request.resolution,
            quality=request.quality,
            background_blur=request.background_blur,
            blur_strength=request.blur_strength,
        )
        short_data = create_short(render_request)

        return {
            "success": True,
            "best": best,
            "ai_enabled": True,
            "ai_model": AI_MODEL,
            "ai_message": "Using the #1 moment already selected by MAIGO AI.",
            "content_package": {"hook": request.hook, "caption": request.caption, "hashtags": request.hashtags[:6]},
            "short": short_data,
            "message": "Best Short created successfully.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        print("[MAIGO] BEST SHORT ERROR:", repr(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/create-short")
def create_short(request: ShortRequest):
    temp_ass = None

    try:
        video = find_video_file(request.file_id)
        if video is None:
            raise RuntimeError("Source video not found.")

        if request.start < 0 or request.end <= request.start:
            raise RuntimeError("Invalid short range.")

        source_info = get_video_info(video)
        duration = source_info["duration_seconds"]

        if request.start >= duration:
            raise RuntimeError("Start time is beyond the video duration.")

        end = min(request.end, duration)
        short_duration = end - request.start

        resolution = request.resolution if request.resolution in {"720", "1080"} else "1080"
        quality_map = {"fast": (26, "veryfast"), "balanced": (22, "veryfast"), "high": (19, "faster")}
        crf, preset = quality_map.get(request.quality, quality_map["balanced"])
        target_w = 1080 if resolution == "1080" else 720
        target_h = 1920 if resolution == "1080" else 1280

        short_id = str(uuid.uuid4())
        output = SHORT_DIR / f"{short_id}.mp4"

        attention_video = None
        if request.attention_video_id:
            attention_video = find_video_file(request.attention_video_id)
            if attention_video is None:
                raise RuntimeError(
                    "Attention gameplay video was not found. Upload it again."
                )

        subtitle = find_subtitle_file(request.file_id)
        subtitle_count = 0

        if subtitle:
            temp_ass = SHORT_DIR / f"{short_id}.ass"
            subtitle_count = create_ass(
                subtitle,
                request.start,
                end,
                temp_ass,
                request.caption_style,
                request.caption_position,
                request.caption_size,
                request.hook_text,
                request.hook_overlay,
            )

        filters = []
        blur_on = bool(request.background_blur)
        blur_sigma = max(6, min(35, int(request.blur_strength or 18)))

        if attention_video is not None:
            # Split-screen layout. When blur is enabled, each panel gets a
            # blurred fill behind the sharp source so the full source framing
            # is preserved instead of hard-cropping away the sides.
            split_bottom_h = int(round(target_h * 840 / 1920))
            split_top_h = target_h - split_bottom_h

            input_args = [
                "-ss", str(request.start),
                "-i", str(video),
                "-stream_loop", "-1",
                "-i", str(attention_video),
            ]

            if blur_on:
                filters.extend([
                    f"[0:v]scale={target_w}:{split_top_h}:force_original_aspect_ratio=increase,crop={target_w}:{split_top_h},gblur=sigma={blur_sigma}:steps=1[topbg]",
                    f"[0:v]scale={target_w}:{split_top_h}:force_original_aspect_ratio=decrease[topfg]",
                    f"[topbg][topfg]overlay=(W-w)/2:(H-h)/2[top]",
                    f"[1:v]scale={target_w}:{split_bottom_h}:force_original_aspect_ratio=increase,crop={target_w}:{split_bottom_h},gblur=sigma={blur_sigma}:steps=1[botbg]",
                    f"[1:v]scale={target_w}:{split_bottom_h}:force_original_aspect_ratio=decrease[botfg]",
                    f"[botbg][botfg]overlay=(W-w)/2:(H-h)/2[bottom]",
                    f"color=c=black:s={target_w}x{target_h}[canvas]",
                    f"[canvas][top]overlay=0:0[canvas2]",
                    f"[canvas2][bottom]overlay=0:{split_top_h}[v0]",
                ])
            else:
                # Existing smart/center crop behavior remains available.
                if request.smart_framing:
                    top_crop_base = build_motion_crop_filter(video, request.start, end, 1.0)
                else:
                    top_crop_base = (
                        "scale=1080:1080:"
                        "force_original_aspect_ratio=increase,crop=1080:1080"
                    )
                top_crop = top_crop_base.replace("scale=1080:1080", f"scale={target_w}:{target_w}")
                bottom_crop = f"scale={target_w}:{split_bottom_h}:force_original_aspect_ratio=increase,crop={target_w}:{split_bottom_h}"
                filters.extend([
                    f"[0:v]{top_crop}[top]",
                    f"[1:v]{bottom_crop}[bottom]",
                    f"color=c=black:s={target_w}x{target_h}[canvas]",
                    f"[canvas][top]overlay=0:0[canvas2]",
                    f"[canvas2][bottom]overlay=0:{split_top_h}[v0]",
                ])
        else:
            # Full-screen vertical Short.
            input_args = [
                "-ss", str(request.start),
                "-i", str(video),
            ]

            if blur_on:
                # Preserve the original video inside a vertical canvas and
                # use a blurred copy behind it. For the usual landscape source
                # this creates the desired blurred top/bottom fill.
                filters.extend([
                    f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,crop={target_w}:{target_h},gblur=sigma={blur_sigma}:steps=1[bg]",
                    f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg]",
                    f"[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]",
                ])
            else:
                if request.smart_framing:
                    vertical_crop_base = build_motion_crop_filter(video, request.start, end, 9.0 / 16.0)
                    vertical_crop = vertical_crop_base.replace("scale=1080:1920", f"scale={target_w}:{target_h}")
                else:
                    vertical_crop = (
                        f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                        f"crop={target_w}:{target_h}"
                    )
                filters.append(f"[0:v]{vertical_crop}[v0]")

        last_video = "[v0]"

        if temp_ass is not None and subtitle_count > 0:
            filters.append(
                f"[v0]ass='{escape_filter_path(temp_ass)}'[v]"
            )
            last_video = "[v]"

        filter_complex = ";".join(filters)

        command = [
            FFMPEG,
            "-y",
            *input_args,
            "-t", str(short_duration),
            "-filter_complex", filter_complex,
            "-map", last_video,
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-shortest",
            str(output),
        ]

        print(
            "[MAIGO] Creating 9:16 Short",
            f"mode={request.attention_mode}",
            f"attention={'yes' if attention_video else 'no'}",
            f"smart_framing={'yes' if request.smart_framing else 'no'}",
            f"caption_style={request.caption_style}",
            f"resolution={resolution}",
            f"quality={request.quality}",
            f"background_blur={'yes' if blur_on else 'no'}",
            f"blur_strength={blur_sigma}",
            f"hook_overlay={"yes" if request.hook_overlay and request.hook_text.strip() else "no"}",
        )

        result = run_command(
            command,
            600,
        )

        if result.returncode != 0:
            raise RuntimeError(
                "9:16 short creation failed: "
                + result.stderr
            )

        if not output.exists():
            raise RuntimeError("Short file was not created.")

        info = get_video_info(output)

        return {
            "success": True,
            "short_id": short_id,
            "start": request.start,
            "end": end,
            "duration": round(short_duration, 2),
            "format": "9:16",
            "width": info["width"],
            "height": info["height"],
            "subtitles": subtitle_count > 0,
            "subtitle_cues": subtitle_count,
            "attention_gameplay": attention_video is not None,
            "attention_mode": request.attention_mode,
            "smart_framing": request.smart_framing,
            "caption_style": request.caption_style,
            "caption_position": request.caption_position,
            "caption_size": request.caption_size,
            "hook_overlay": bool(request.hook_overlay and request.hook_text.strip()),
            "hook_text": request.hook_text,
            "resolution": resolution,
            "quality": request.quality,
            "background_blur": blur_on,
            "blur_strength": blur_sigma,
            "word_timed_captions": subtitle_count > 0,
            "framing": (
                "blurred split-screen main + attention gameplay"
                if attention_video and blur_on
                else "motion-aware split-screen main + attention gameplay"
                if attention_video and request.smart_framing
                else "split-screen main + attention gameplay"
                if attention_video
                else "motion-aware 9:16 crop"
                if request.smart_framing
                else "9:16 center crop"
            ),
            "message": "9:16 Short created successfully.",
            "file": f"/shorts/{output.name}",
        }

    except Exception as exc:
        print("[MAIGO] SHORT ERROR:", repr(exc))
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc

    finally:
        if temp_ass is not None:
            temp_ass.unlink(missing_ok=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
