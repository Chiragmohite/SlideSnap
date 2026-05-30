"""
VidToDoc backend: YouTube URL or uploaded video -> slide frames -> Groq vision -> PDF
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
COOKIES_PATH = Path(__file__).resolve().parent / "cookies.txt"
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
    start_time: Optional[str] = None
    end_time:   Optional[str] = None

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
    p = job_path(job_id)
    data = job_get(job_id) or {}
    data.update(kw)
    pdf_bytes = data.pop("pdf_bytes", None)
    p.write_text(json.dumps(data))
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

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_video_id(url: str) -> str:
    m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{6,})", url)
    if m: return m.group(1)
    raise HTTPException(status_code=400, detail="Invalid YouTube URL.")

async def fetch_video_title(url: str, video_id: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json")
            if res.status_code == 200: return res.json().get("title", video_id)
    except: pass
    return video_id

def get_video_duration(video: Path) -> float:
    ffprobe_path = shutil.which("ffprobe") or "ffprobe"
    try:
        proc = subprocess.run([ffprobe_path, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, timeout=30)
        return float(proc.stdout.strip())
    except: return 600.0

def download_video(url: str, out_dir: Path,
                   start_time: Optional[str] = None,
                   end_time: Optional[str] = None) -> Path:
    out_tpl = str(out_dir / "video.%(ext)s")
    cmd = [
        *tool_cmd("yt-dlp"), "--no-playlist",
        "-f", "bv*[height<=480]+ba/b[height<=480]/best[height<=480]",
        "--merge-output-format", "mp4",
        "--no-check-certificate",
        "--legacy-server-connect",
        "--extractor-retries", "3",
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "-o", out_tpl,
    ]
    if COOKIES_PATH.exists():
        cmd += ["--cookies", str(COOKIES_PATH)]
    if start_time or end_time:
        cmd += ["--download-sections", f"*{start_time or '00:00:00'}-{end_time or '99:59:59'}"]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=f"Download failed: {proc.stderr[-400:]}")
    files = list(out_dir.glob("video.*"))
    if not files: raise HTTPException(status_code=502, detail="No video file after download.")
    return files[0]

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

def image_to_data_url(path: Path) -> str:
    b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"

async def groq_extract_slide(api_key: str, image_path: Path) -> str:
    payload = {"model": VISION_MODEL, "messages": [{"role": "user", "content": [
        {"type": "text", "text": SLIDE_PROMPT},
        {"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}},
    ]}], "max_tokens": 1800}
    msg = ""
    for attempt in range(3):
        async with httpx.AsyncClient(timeout=120.0) as client:
            res = await client.post(GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload)
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

def skip_slide_text(text: str) -> bool:
    return text.strip().lower() in ("[no text on slide]", "[no text on slide].", "")

async def consolidate_text(api_key: str, raw_slides: list[tuple[int, str]]) -> str:
    combined = "\n\n".join(f"[Slide {n}]\n{t}" for n, t in raw_slides)
    payload = {"model": VISION_MODEL, "messages": [{"role": "user", "content": (
        "Below is raw text extracted from frames of an educational video. "
        "Frames may overlap or repeat due to slow scrolling or transitions. "
        "Your job: produce ONE clean, deduplicated study document. "
        "Preserve ALL module/chapter/unit/topic headings exactly as they appear. "
        "Keep questions under the heading they appeared after -- never merge two sections. "
        "Questions appearing before any heading go under the first heading found, "
        "or 'General' if no headings exist at all. "
        "If a section has no questions, skip it entirely -- do not write 'No questions'. "
        "Handle any subject: math, science, engineering, history, coding, law, medicine, etc. "
        "Number questions within each section starting from 1. "
        "Remove watermarks, social media handles, branding, YouTube UI elements. "
        "Output only the final clean question list -- no explanation, no preamble.\n\n" + combined
    )}], "max_tokens": 2000}
    async with httpx.AsyncClient(timeout=120.0) as client:
        res = await client.post(GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload)
    if res.status_code != 200:
        try: msg = res.json().get("error", {}).get("message", res.text)
        except: msg = res.text
        raise HTTPException(status_code=502, detail=f"Groq consolidation error: {msg}")
    return res.json()["choices"][0]["message"]["content"].strip()

def build_pdf(url: str, title: str, text: str, out_path: Path) -> None:
    styles = getSampleStyleSheet()
    title_style   = ParagraphStyle("T", parent=styles["Heading1"], fontSize=16, spaceAfter=4)
    meta_style    = ParagraphStyle("M", parent=styles["Normal"],   fontSize=9,  textColor="#555555", spaceAfter=14)
    heading_style = ParagraphStyle("H", parent=styles["Heading2"], fontSize=12, spaceBefore=14, spaceAfter=5)
    body_style    = ParagraphStyle("B", parent=styles["Normal"],   fontSize=10, leading=16, spaceAfter=5)
    def esc(s): return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    doc = SimpleDocTemplate(str(out_path), pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm, topMargin=16*mm, bottomMargin=16*mm,
        title=title, author="SlideSnap", subject="Study Notes")
    story = [Paragraph(esc(title), title_style), Paragraph(esc(f"Source: {url}"), meta_style), Spacer(1, 6)]
    for line in text.splitlines():
        s = line.strip()
        if not s: story.append(Spacer(1, 4))
        elif re.match(r"(?i)^(module|chapter|unit|topic|section|part|general)[\s\-]*[\d\w]?", s) or re.match(r"^#{1,3}\s", s):
            story.append(Paragraph(esc(re.sub(r"^#{1,3}\s*", "", s)), heading_style))
        else: story.append(Paragraph(esc(s), body_style))
    doc.build(story)

# ── Job runner ────────────────────────────────────────────────────────────────

async def process_video(job_id: str, video_path: Path, title: str, source_url: str, api_key: str):
    try:
        job_set(job_id, step="Extracting frames...", pct=25)
        frames_dir = Path(tempfile.mkdtemp(prefix="frames_"))
        raw_frames = extract_frames(video_path, frames_dir)
        if not raw_frames: raise HTTPException(status_code=502, detail="No frames extracted.")

        job_set(job_id, step="Removing duplicate frames...", pct=35)
        unique_frames = dedupe_frames(raw_frames)
        if not unique_frames: raise HTTPException(status_code=502, detail="No usable frames.")

        total = len(unique_frames)
        slides, slide_num = [], 0
        for i, frame_path in enumerate(unique_frames):
            pct = 35 + int((i / total) * 45)
            job_set(job_id, step=f"Reading frame {i+1} of {total}...", pct=pct)
            text = await groq_extract_slide(api_key, frame_path)
            if not skip_slide_text(text):
                slide_num += 1
                slides.append((slide_num, text))
            await asyncio.sleep(8)

        if not slides: raise HTTPException(status_code=502, detail="No text found in video frames.")

        job_set(job_id, step="Cleaning and organising content...", pct=82)
        await asyncio.sleep(8)
        clean_text = await consolidate_text(api_key, slides)

        job_set(job_id, step="Building PDF...", pct=93)
        out_pdf = Path(tempfile.mktemp(suffix=".pdf"))
        build_pdf(source_url, title, clean_text, out_pdf)
        pdf_bytes = out_pdf.read_bytes()

        safe_title = re.sub(r"[^a-zA-Z0-9 _-]", "", title).strip().replace(" ", "_")[:60]
        filename = f"{safe_title or 'notes'}.pdf"
        job_set(job_id, status="done", step="Done!", pct=100, pdf_bytes=pdf_bytes, filename=filename)

        # Cleanup
        shutil.rmtree(frames_dir, ignore_errors=True)
        out_pdf.unlink(missing_ok=True)

    except HTTPException as e:
        job_set(job_id, status="error", step="Failed", error=e.detail)
    except Exception as e:
        job_set(job_id, status="error", step="Failed", error=str(e))

async def run_job_url(job_id: str, url: str, api_key: str,
                      start_time: Optional[str], end_time: Optional[str]):
    try:
        job_set(job_id, status="processing", step="Fetching video info...", pct=5)
        video_id = parse_video_id(url)
        title    = await fetch_video_title(url, video_id)
        range_label = f" [{start_time or 'start'} → {end_time or 'end'}]" if (start_time or end_time) else ""
        job_set(job_id, step=f"Downloading: {title}{range_label}...", pct=10)

        with tempfile.TemporaryDirectory(prefix="vidtodoc_") as tmp:
            work = Path(tmp)
            video_file = download_video(url, work, start_time, end_time)
            await process_video(job_id, video_file, title, url, api_key)
    except HTTPException as e:
        job_set(job_id, status="error", step="Failed", error=e.detail)
    except Exception as e:
        job_set(job_id, status="error", step="Failed", error=str(e))

async def run_job_file(job_id: str, video_path: Path, filename: str, api_key: str):
    try:
        job_set(job_id, status="processing", step="Processing uploaded video...", pct=15)
        title = Path(filename).stem.replace("_", " ").replace("-", " ")
        await process_video(job_id, video_path, title, f"Uploaded: {filename}", api_key)
    finally:
        video_path.unlink(missing_ok=True)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    p = ROOT / "index.html"
    if not p.exists(): raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(p)

@app.get("/api/health")
async def health():
    return {"ok": True, "api_key_set": bool(os.environ.get("GROQ_API_KEY")),
            "cookies_found": COOKIES_PATH.exists(),
            "ffmpeg": tool_status("ffmpeg"), "yt_dlp": tool_status("yt-dlp")}

@app.post("/api/generate")
async def generate(req: GenerateRequest):
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key: raise HTTPException(status_code=500, detail="GROQ_API_KEY not set.")
    resolve_tool("ffmpeg")
    job_id = str(uuid.uuid4())
    job_set(job_id, status="pending", step="Starting...", pct=0, filename=None, error=None)
    asyncio.create_task(run_job_url(job_id, req.url.strip(), api_key, req.start_time, req.end_time))
    return {"job_id": job_id}

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key: raise HTTPException(status_code=500, detail="GROQ_API_KEY not set.")
    resolve_tool("ffmpeg")

    # Check size
    contents = await file.read()
    size_mb = len(contents) / 1024 / 1024
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(status_code=413, detail=f"File too large ({size_mb:.0f}MB). Max {MAX_UPLOAD_MB}MB.")

    # Save to temp file
    suffix = Path(file.filename).suffix or ".mp4"
    tmp_path = Path(tempfile.mktemp(prefix="upload_", suffix=suffix))
    tmp_path.write_bytes(contents)

    job_id = str(uuid.uuid4())
    job_set(job_id, status="pending", step="Starting...", pct=0, filename=None, error=None)
    asyncio.create_task(run_job_file(job_id, tmp_path, file.filename or "video.mp4", api_key))
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