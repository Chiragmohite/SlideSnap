# 📄 SlideSnap

**Turn any educational YouTube video into a clean, downloadable study PDF — powered by AI.**

VidToDoc extracts slide frames from YouTube videos, reads the text using vision AI, and generates a well-formatted PDF with questions and notes organized by module/topic.

---

## ✨ Features

- 🎯 Paste a YouTube link → get a study PDF in minutes
- 🧠 AI reads every slide frame and extracts questions, answers, and notes
- 🗂️ Auto-organizes content by Module / Chapter / Topic
- 🔁 Removes duplicate frames from slow-scrolling videos
- 📥 Clean downloadable PDF with proper headings and numbering
- 🌐 Works for any subject — engineering, science, history, coding, law, medicine

---

## 🖥️ Demo

> Paste a YouTube video URL → AI scans the slides → Download your PDF

![VidToDoc UI](https://raw.githubusercontent.com/Chiragmohite/VidToDoc/main/screenshot.png)

---

## 🚀 Getting Started

### Prerequisites

- Python 3.10+
- [FFmpeg](https://www.ffmpeg.org/download.html) — install via: `winget install Gyan.FFmpeg`
- A free [Groq API key](https://console.groq.com) (uses `llama-4-scout` vision model)

### Installation

```bash
# 1. Clone the repo
git clone https://github.com/Chiragmohite/VidToDoc.git
cd VidToDoc

# 2. Install dependencies
pip install -r backend/requirements.txt

# 3. Add your Groq API key
cp backend/.env.example backend/.env
# Edit backend/.env and paste your key:
# GROQ_API_KEY=your_key_here

# 4. Start the server
cd backend
python main.py
```

Then open **http://127.0.0.1:8000** in your browser.

---

## 🔧 How It Works

```
YouTube URL
    ↓
Download video (yt-dlp)
    ↓
Extract frames — scene detection + periodic + dense early sampling (FFmpeg)
    ↓
Deduplicate similar frames (perceptual hashing)
    ↓
Read each frame with Groq vision AI (llama-4-scout)
    ↓
Consolidate + clean with AI — remove duplicates, group by module
    ↓
Generate formatted PDF (ReportLab)
    ↓
Download ✅
```

---

## 📁 Project Structure

```
VidToDoc/
├── index.html          # Frontend UI
├── start.bat           # Windows quick-start script
├── backend/
│   ├── main.py         # FastAPI backend
│   ├── requirements.txt
│   ├── .env            # Your API key (never committed)
│   └── .env.example    # Template
└── README.md
```

---

## ⚙️ Configuration

| Variable | Description |
|---|---|
| `GROQ_API_KEY` | Your Groq API key from console.groq.com |

Set it in `backend/.env`:
```
GROQ_API_KEY=your_key_here
```

---

## ⚠️ Notes

- Processing takes **3–5 minutes** depending on video length (due to Groq free tier rate limits)
- Works best with slide-based or text-heavy educational videos
- Auto-captions/transcript videos may have lower accuracy for handwritten content
- Groq free tier: 500k tokens/day — enough for ~10 videos per day

---

## 🛠️ Built With

- [FastAPI](https://fastapi.tiangolo.com/) — backend API
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — video downloading
- [FFmpeg](https://ffmpeg.org/) — frame extraction
- [Groq](https://groq.com/) — vision AI (llama-4-scout)
- [ReportLab](https://www.reportlab.com/) — PDF generation
- [imagehash](https://github.com/JohannesBuchner/imagehash) — frame deduplication

---

## 🙋 Author

Made by [Chirag Mohite](https://github.com/Chiragmohite)
