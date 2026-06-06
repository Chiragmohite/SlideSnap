"""
VidToDoc backend: YouTube transcript -> Groq AI -> PDF
Falls back to video file upload if transcript unavailable.
"""
from __future__ import annotations
import asyncio, base64, json, os, re, shutil, subprocess, sys, tempfile, uuid
from pathlib import Path
from typing import Optional
import httpx, imagehash
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from PIL import Image
from pydantic import BaseModel, Field
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

load_dotenv(Path(__file__).resolve().parent / ".env")

ROOT         = Path(__file__).resolve().parent.parent
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
MAX_SLIDES   = 50
PHASH_THRESHOLD = 10
MAX_UPLOAD_MB = 500

JOBS_DIR = Path("/tmp/slidesnap_jobs")
JOBS_DIR.mkdir(exist_ok=True)

SLIDE_PROMPT = (
    "This is a frame from an educational video. "
    "Copy ALL visible text exactly: questions, options, answers, formulas, "
    "headings, bullets, definitions, code snippets. "
    "Output ONLY the text with no preamble like 'The image contains'. "
    "If something is unreadable write [unclear]. "
    "If there is no educational text at all, reply exactly: [no text on slide]"
)

app = FastAPI(title="VidToDoc API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

class GenerateRequest(BaseModel):
    url: str = Field(..., min_length=10)

_TOOL_CACHE: dict[str, list[str]] = {}

# ── Job store ─────────────────────────────────────────────────────────────────

def job_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"

def pdf_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.pdf"

def job_get(job_id: str) -> Optional[dict]:
    p = job_path(job_id)
    if not p.exists(): return None
    try: return json.loads(p.read_text())
    except: return None

def job_set(job_id: str, **kw):
    data = job_get(job_id) or {}
    data.update(kw)
    pdf_bytes = data.pop("pdf_bytes", None)
    job_path(job_id).write_text(json.dumps(data))
    if pdf_bytes is not None:
        pdf_path(job_id).write_bytes(pdf_bytes)

# ── Tool resolution ───────────────────────────────────────────────────────────

def resolve_tool(name: str) -> list[str]:
    if name in _TOOL_CACHE: return _TOOL_CACHE[name]
    found = shutil.which(name)
    if found:
        _TOOL_CACHE[name] = [found]
        return _TOOL_CACHE[name]
    if name == "yt-dlp":
        for cmd in ([sys.executable, "-m", "yt_dlp"], ["py", "-3", "-m", "yt_dlp"]):
            try:
                proc = subprocess.run([*cmd, "--version"], capture_output=True, timeout=15)
                if proc.returncode == 0:
                    _TOOL_CACHE[name] = cmd
                    return _TOOL_CACHE[name]
            except (FileNotFoundError, subprocess.TimeoutExpired): continue
    hint = "Install ffmpeg via apt." if name == "ffmpeg" else "Install: pip install yt-dlp"
    raise HTTPException(status_code=500, detail=f"'{name}' not found. {hint}")

def tool_cmd(name: str) -> list[str]: return resolve_tool(name)
def tool_status(name: str) -> dict:
    try: return {"available": True, "cmd": resolve_tool(name)}
    except HTTPException as e: return {"available": False, "error": getattr(e, "detail", "not found")}

# ── YouTube helpers ───────────────────────────────────────────────────────────

def parse_video_id(url: str) -> str:
    m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{6,})", url)
    if m: return m.group(1)
    raise HTTPException(status_code=400, detail="Invalid YouTube URL.")

async def fetch_video_title(video_id: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
            )
            if res.status_code == 200:
                return res.json().get("title", video_id)
    except: pass
    return video_id

async def fetch_transcript(video_id: str) -> Optional[str]:
    """Fetch YouTube transcript using youtube-transcript-api."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
        loop = asyncio.get_event_loop()
        def _fetch():
            try:
                transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
                # Try English first, then any available
                try:
                    transcript = transcript_list.find_transcript(['en', 'en-US', 'en-GB', 'en-IN'])
                except:
                    transcript = transcript_list.find_generated_transcript(['en', 'en-US', 'en-GB', 'en-IN'])
                data = transcript.fetch()
                # Combine all text with timestamps
                lines = []
                for entry in data:
                    text = entry.get('text', '').strip()
                    if text and text != '[Music]':
                        lines.append(text)
                return ' '.join(lines)
            except (NoTranscriptFound, TranscriptsDisabled):
                return None
            except Exception:
                return None
        return await loop.run_in_executor(None, _fetch)
    except ImportError:
        return None

# ── Groq helpers ──────────────────────────────────────────────────────────────

async def organize_transcript(api_key: str, transcript: str, title: str) -> str:
    """Use Groq AI to organize raw transcript into study notes."""
    # Truncate if too long
    if len(transcript) > 12000:
        transcript = transcript[:12000] + "..."

    payload = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": (
            f"The following is a transcript from a YouTube lecture titled: '{title}'\n\n"
            "Your job: Convert this raw transcript into clean, organized study notes.\n"
            "- Identify and preserve all module/chapter/topic headings\n"
            "- Extract all questions and their answers\n"
            "- Keep important definitions, formulas, and concepts\n"
            "- Group content under proper headings\n"
            "- Number questions within each section starting from 1\n"
            "- Remove filler words, repetitions, and non-educational content\n"
            "- Output only the final clean study notes, no explanation or preamble\n\n"
            f"TRANSCRIPT:\n{transcript}"
        )}],
        "max_tokens": 3000,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    if res.status_code != 200:
        try: msg = res.json().get("error", {}).get("message", res.text)
        except: msg = res.text
        raise HTTPException(status_code=502, detail=f"Groq error: {msg}")
    return res.json()["choices"][0]["message"]["content"].strip()

async def groq_extract_slide(api_key: str, image_path: Path) -> str:
    payload = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": SLIDE_PROMPT},
            {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
        ]}],
        "max_tokens": 1800,
    }
    msg = ""
    for attempt in range(3):
        async with httpx.AsyncClient(timeout=120.0) as client:
            res = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
        if res.status_code == 200: break
        try: msg = res.json().get("error", {}).get("message", res.text)
        except: msg = res.text
        if res.status_code == 429:
            await asyncio.sleep(65 if "per day" in msg else 15)
            continue
        raise HTTPException(status_code=502, detail=f"Groq API error: {msg}")
    else:
        raise HTTPException(status_code=502, detail=f"Groq API error after retries: {msg}")
    data = res.json()
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    text = re.sub(r"(?i)^the image contains the following text[:\s]*", "", (text or "").strip()).strip()
    return text or "[no text on slide]"

async def consolidate_text(api_key: str, raw_slides: list[tuple[int, str]]) -> str:
    combined = "\n\n".join(f"[Slide {n}]\n{t}" for n, t in raw_slides)
    payload = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": (
            "Below is raw text extracted from frames of an educational video. "
            "Your job: produce ONE clean, deduplicated study document. "
            "Preserve ALL module/chapter/unit/topic headings exactly as they appear. "
            "Keep questions under the heading they appeared after. "
            "Number questions within each section starting from 1. "
            "Remove watermarks, social media handles, branding, YouTube UI elements. "
            "Output only the final clean question list -- no explanation, no preamble.\n\n"
            + combined
        )}],
        "max_tokens": 2000,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    if res.status_code != 200:
        try: msg = res.json().get("error", {}).get("message", res.text)
        except: msg = res.text
        raise HTTPException(status_code=502, detail=f"Groq consolidation error: {msg}")
    return res.json()["choices"][0]["message"]["content"].strip()

# ── Video processing helpers ──────────────────────────────────────────────────

def image_to_data_url(path: Path) -> str:
    b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"

def skip_slide_text(text: str) -> bool:
    return text.strip().lower() in ("[no text on slide]", "[no text on slide].", "")

def get_video_duration(video: Path) -> float:
    ffprobe_path = shutil.which("ffprobe") or "ffprobe"
    try:
        proc = subprocess.run(
            [ffprobe_path, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, timeout=30)
        return float(proc.stdout.strip())
    except: return 600.0

def extract_frames(video: Path, frames_dir: Path) -> list[Path]:
    scene_pattern = str(frames_dir / "scene_%04d.jpg")
    subprocess.run([*tool_cmd("ffmpeg"), "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-vf", "select='gt(scene,0.20)',scale=960:-1",
        "-vsync", "vfr", "-frames:v", str(MAX_SLIDES), scene_pattern],
        capture_output=True, timeout=600)
    frames = sorted(frames_dir.glob("scene_*.jpg"))

    duration = get_video_duration(video)
    interval = max(3, int(duration / MAX_SLIDES))
    periodic_pattern = str(frames_dir / "sec_%04d.jpg")
    subprocess.run([*tool_cmd("ffmpeg"), "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-vf", f"fps=1/{interval},scale=960:-1", periodic_pattern],
        capture_output=True, timeout=600)
    periodic_frames = sorted(frames_dir.glob("sec_*.jpg"))

    early_pattern = str(frames_dir / "early_%04d.jpg")
    subprocess.run([*tool_cmd("ffmpeg"), "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-t", "90", "-vf", "fps=1/4,scale=960:-1", early_pattern],
        capture_output=True, timeout=120)
    early_frames = sorted(frames_dir.glob("early_*.jpg"))

    all_frames = sorted(set(frames) | set(periodic_frames) | set(early_frames))
    return all_frames[:MAX_SLIDES * 2]

def dedupe_frames(frame_paths: list[Path]) -> list[Path]:
    kept, last_hash = [], None
    for path in frame_paths:
        try:
            img = Image.open(path).convert("RGB")
            h = imagehash.phash(img)
        except: continue
        if last_hash is not None and (h - last_hash) <= PHASH_THRESHOLD: continue
        last_hash = h
        kept.append(path)
    return kept[:MAX_SLIDES]

# ── PDF builder ───────────────────────────────────────────────────────────────

def build_pdf(source: str, title: str, text: str, out_path: Path) -> None:
    styles = getSampleStyleSheet()
    title_style   = ParagraphStyle("T", parent=styles["Heading1"], fontSize=16, spaceAfter=4)
    meta_style    = ParagraphStyle("M", parent=styles["Normal"],   fontSize=9,  textColor="#555555", spaceAfter=14)
    heading_style = ParagraphStyle("H", parent=styles["Heading2"], fontSize=12, spaceBefore=14, spaceAfter=5)
    body_style    = ParagraphStyle("B", parent=styles["Normal"],   fontSize=10, leading=16, spaceAfter=5)
    def esc(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    doc = SimpleDocTemplate(str(out_path), pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm, topMargin=16*mm, bottomMargin=16*mm,
        title=title, author="SlideSnap", subject="Study Notes")
    story = [Paragraph(esc(title), title_style), Paragraph(esc(f"Source: {source}"), meta_style), Spacer(1, 6)]
    for line in text.splitlines():
        s = line.strip()
        if not s: story.append(Spacer(1, 4))
        elif re.match(r"(?i)^(module|chapter|unit|topic|section|part|general)[\s\-]*[\d\w]?", s) or re.match(r"^#{1,3}\s", s):
            story.append(Paragraph(esc(re.sub(r"^#{1,3}\s*", "", s)), heading_style))
        else: story.append(Paragraph(esc(s), body_style))
    doc.build(story)

def finalize_job(job_id: str, title: str, source: str, clean_text: str):
    out_pdf = JOBS_DIR / f"{job_id}_out.pdf"
    build_pdf(source, title, clean_text, out_pdf)
    pdf_bytes = out_pdf.read_bytes()
    out_pdf.unlink(missing_ok=True)
    safe_title = re.sub(r"[^a-zA-Z0-9 _-]", "", title).strip().replace(" ", "_")[:60]
    filename = f"{safe_title or 'notes'}.pdf"
    job_set(job_id, status="done", step="Done!", pct=100, pdf_bytes=pdf_bytes, filename=filename)

# ── Job runners ───────────────────────────────────────────────────────────────

async def run_job_url(job_id: str, url: str, api_key: str):
    try:
        job_set(job_id, status="processing", step="Fetching video info...", pct=5)
        video_id = parse_video_id(url)
        title    = await fetch_video_title(video_id)

        # Try transcript first
        job_set(job_id, step="Looking for transcript...", pct=15)
        transcript = await fetch_transcript(video_id)

        if transcript:
            job_set(job_id, step="Transcript found! Organising with AI...", pct=40)
            clean_text = await organize_transcript(api_key, transcript, title)
            job_set(job_id, step="Building PDF...", pct=90)
            finalize_job(job_id, title, url, clean_text)
        else:
            # No transcript — tell user to upload
            job_set(job_id, status="error", step="Failed",
                error="No transcript found for this video. Please use the 📁 Upload Video tab instead — download the video and upload it directly.")

    except HTTPException as e:
        job_set(job_id, status="error", step="Failed", error=e.detail)
    except Exception as e:
        job_set(job_id, status="error", step="Failed", error=str(e))

async def run_job_upload(job_id: str, video_path: Path, orig_filename: str, api_key: str):
    try:
        job_set(job_id, status="processing", step="Processing uploaded video...", pct=10)
        title = Path(orig_filename).stem.replace("_", " ").replace("-", " ")

        job_set(job_id, step="Extracting frames...", pct=20)
        frames_dir = video_path.parent / "frames"
        frames_dir.mkdir(exist_ok=True)
        raw_frames = extract_frames(video_path, frames_dir)
        if not raw_frames:
            raise HTTPException(status_code=502, detail="No frames extracted.")

        job_set(job_id, step="Removing duplicate frames...", pct=30)
        unique_frames = dedupe_frames(raw_frames)
        if not unique_frames:
            raise HTTPException(status_code=502, detail="No usable frames.")

        total = len(unique_frames)
        slides, slide_num = [], 0
        for i, frame_path in enumerate(unique_frames):
            pct = 30 + int((i / total) * 50)
            job_set(job_id, step=f"Reading frame {i+1} of {total}...", pct=pct)
            text = await groq_extract_slide(api_key, frame_path)
            if not skip_slide_text(text):
                slide_num += 1
                slides.append((slide_num, text))
            await asyncio.sleep(8)

        if not slides:
            raise HTTPException(status_code=502, detail="No text found in video frames.")

        job_set(job_id, step="Cleaning and organising content...", pct=82)
        await asyncio.sleep(8)
        clean_text = await consolidate_text(api_key, slides)

        job_set(job_id, step="Building PDF...", pct=93)
        finalize_job(job_id, title, f"Uploaded: {orig_filename}", clean_text)

    except HTTPException as e:
        job_set(job_id, status="error", step="Failed", error=e.detail)
    except Exception as e:
        job_set(job_id, status="error", step="Failed", error=str(e))
    finally:
        try: shutil.rmtree(video_path.parent, ignore_errors=True)
        except: pass

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    p = ROOT / "index.html"
    if not p.exists(): raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(p)

@app.get("/api/health")
async def health():
    return {"ok": True, "api_key_set": bool(os.environ.get("GROQ_API_KEY")),
            "ffmpeg": tool_status("ffmpeg"), "yt_dlp": tool_status("yt-dlp")}

@app.post("/api/generate")
async def generate(req: GenerateRequest):
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key: raise HTTPException(status_code=500, detail="GROQ_API_KEY not set.")
    job_id = str(uuid.uuid4())
    job_set(job_id, status="pending", step="Starting...", pct=0, filename=None, error=None)
    asyncio.create_task(run_job_url(job_id, req.url.strip(), api_key))
    return {"job_id": job_id}

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key: raise HTTPException(status_code=500, detail="GROQ_API_KEY not set.")
    resolve_tool("ffmpeg")
    contents = await file.read()
    size_mb = len(contents) / 1024 / 1024
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(status_code=413, detail=f"File too large ({size_mb:.0f}MB). Max {MAX_UPLOAD_MB}MB.")
    suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
    tmp_dir = Path(tempfile.mkdtemp(prefix="upload_"))
    tmp_path = tmp_dir / f"video{suffix}"
    tmp_path.write_bytes(contents)
    job_id = str(uuid.uuid4())
    job_set(job_id, status="pending", step="Starting...", pct=0, filename=None, error=None)
    asyncio.create_task(run_job_upload(job_id, tmp_path, file.filename or "video.mp4", api_key))
    return {"job_id": job_id}

@app.get("/api/status/{job_id}")
async def status(job_id: str):
    job = job_get(job_id)
    if not job: raise HTTPException(status_code=404, detail="Job not found.")
    return {"status": job.get("status"), "step": job.get("step"),
            "pct": job.get("pct"), "filename": job.get("filename"), "error": job.get("error")}

@app.get("/api/download/{job_id}")
async def download(job_id: str):
    job = job_get(job_id)
    if not job or job.get("status") != "done": raise HTTPException(status_code=404, detail="PDF not ready.")
    p = pdf_path(job_id)
    if not p.exists(): raise HTTPException(status_code=404, detail="PDF file missing.")
    return Response(content=p.read_bytes(), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{job.get("filename", "notes.pdf")}"'})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)