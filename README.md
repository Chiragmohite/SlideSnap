# VidToDoc

Paste a **YouTube slide video URL** → get a **PDF** with the text from each slide (questions, options, notes on screen).

## Why a backend?

Browsers cannot read YouTube video frames (security/CORS). To go from **link only** → **PDF**, a small local server:

1. Downloads the video (`yt-dlp`)
2. Pulls slide frames (`ffmpeg`)
3. Reads text with Groq vision
4. Builds the PDF

## Setup (Windows)

### 1. Install tools

- **Python 3.10+** — [python.org](https://www.python.org/downloads/)
- **ffmpeg** — `winget install Gyan.FFmpeg` then **restart the server** (close terminal, run `start.bat` again). The app auto-finds winget installs even if PATH was not updated.
- **yt-dlp** — `pip install yt-dlp` (also installed via requirements below)

### 2. Install Python packages

```powershell
cd "C:\Users\Chirag M\Projects\VidToDoc\backend"
pip install -r requirements.txt
```

### 3. Run the app

```powershell
cd "C:\Users\Chirag M\Projects\VidToDoc\backend"
python main.py
```

Open: **http://127.0.0.1:8000**

1. Save your Groq API key
2. Paste YouTube URL (e.g. `https://youtu.be/plKKUgVl73k`)
3. Click **Create PDF from link**
4. Wait (long videos can take several minutes)
5. PDF downloads automatically

## Notes

- First run may be slow (download + many Groq calls).
- Groq API usage depends on number of unique slides detected.
- For hosting online later, deploy this backend (Railway, Render, VPS) — not `file://` HTML alone.
