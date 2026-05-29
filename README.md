---
title: Slidesnap
emoji: 📸
colorFrom: blue
colorTo: purple
sdk: docker
pinned: false
---

# 📸 SlideSnap

**Snap the slides from any YouTube lecture — get a clean, downloadable study PDF.**

SlideSnap extracts slide frames from YouTube videos, reads the text using vision AI, and generates a well-formatted PDF with questions and notes organized by module/topic.

> 🔗 **Try it out:** https://ching-nonprofit-underhandedly.ngrok-free.dev
---

## ✨ Features

- 🎯 Paste a YouTube link → get a study PDF in minutes
- 🧠 AI reads every slide frame and extracts questions, answers, and notes
- 🗂️ Auto-organizes content by Module / Chapter / Topic
- 🔁 Removes duplicate frames from slow-scrolling videos
- 📥 Clean downloadable PDF with proper headings and numbering
- 🌐 Works for any subject — engineering, science, history, coding, law, medicine

---

## 🖥️ Screenshot

![SlideSnap UI](https://raw.githubusercontent.com/Chiragmohite/SlideSnap/main/Screenshot.png.png)

---

## 🚀 Getting Started

### Prerequisites

- Python 3.10+
- [FFmpeg](https://www.ffmpeg.org/download.html) — install via: `winget install Gyan.FFmpeg`
- A free [Groq API key](https://console.groq.com) (uses `llama-4-scout` vision model)

### Installations

```bash
# 1. Clone the repo
git clone https://github.com/Chiragmohite/SlideSnap.git
cd SlideSnap

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
SlideSnap/
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

Set your Groq API key in `backend/.env`:
```
GROQ_API_KEY=your_key_here
```

Get a free key at [console.groq.com](https://console.groq.com)

---

## ⚠️ Notes

- Processing takes **3–5 minutes** depending on video length (Groq free tier rate limits)
- Works best with slide-based or text-heavy educational videos
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
