"""
Video Downloader API Service
Support: Douyin, Bilibili, Shipinhao, YouTube, etc.
Core engine: yt-dlp
"""

import os
import uuid
import shutil
import urllib3
import threading
from pathlib import Path
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(
    title="Video Downloader API",
    description="Download videos from Douyin, Bilibili, Shipinhao, YouTube etc.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/tmp/video_downloads")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "500"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "120"))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class DownloadRequest(BaseModel):
    url: str
    format: Optional[str] = "best"
    filename: Optional[str] = None


class InfoResponse(BaseModel):
    success: bool
    title: str
    platform: str
    description: str = ""
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    author: Optional[str] = None
    formats: list = []


def identify_platform(url: str) -> str:
    if "douyin.com" in url or "iesdouyin.com" in url:
        return "douyin"
    elif "bilibili.com" in url or "b23.tv" in url:
        return "bilibili"
    elif "weixin.qq.com" in url or "channels" in url:
        return "shipinhao"
    elif "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    elif "tiktok.com" in url:
        return "tiktok"
    else:
        return "general"


def get_ydl_opts(output_path: str, fmt: str = "best") -> dict:
    return {
        "outtmpl": output_path,
        "format": fmt,
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "merge_output_format": "mp4",
        "http_headers": COMMON_HEADERS,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
    }


@app.get("/", summary="Health check")
async def health_check():
    return {"status": "ok", "service": "Video Downloader API", "version": "2.0.0"}


@app.get("/api/info", summary="Get video info")
async def get_video_info(url: str = Query(..., description="Video URL")):
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "http_headers": COMMON_HEADERS,
            "socket_timeout": 30,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        platform = identify_platform(url)

        formats = []
        for f in info.get("formats", [])[:10]:
            formats.append({
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "quality": f.get("height"),
                "filesize": f.get("filesize"),
            })

        return InfoResponse(
            success=True,
            title=info.get("title", ""),
            platform=platform,
            description=(info.get("description") or "")[:500],
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail"),
            author=info.get("uploader") or info.get("channel") or "",
            formats=formats,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to get info: {str(e)}")


@app.post("/api/download", summary="Download video")
async def download_video(req: DownloadRequest):
    task_id = str(uuid.uuid4())[:8]
    task_dir = os.path.join(DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    try:
        info_opts = {
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "http_headers": COMMON_HEADERS,
            "socket_timeout": 30,
        }
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
            title = info.get("title", "unknown")
            description = (info.get("description") or "")[:500]
            duration = info.get("duration")
            ext = info.get("ext", "mp4")
            author = info.get("uploader") or info.get("channel") or ""
            thumbnail = info.get("thumbnail", "")

        platform = identify_platform(req.url)

        if req.filename:
            safe_name = req.filename
        else:
            safe_name = "".join(
                c for c in title if c.isalnum() or c in "._-() "
            )[:80].strip() or f"video_{task_id}"

        output_path = os.path.join(task_dir, f"{safe_name}.%(ext)s")

        ydl_opts = get_ydl_opts(output_path, req.format)

        result_holder = {"error": None, "done": False}

        def do_download():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([req.url])
            except Exception as e:
                result_holder["error"] = e
            finally:
                result_holder["done"] = True

        t = threading.Thread(target=do_download, daemon=True)
        t.start()
        t.join(timeout=DOWNLOAD_TIMEOUT)

        if not result_holder["done"]:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise HTTPException(status_code=408, detail=f"Download timed out after {DOWNLOAD_TIMEOUT}s")

        if result_holder["error"]:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"Download failed: {str(result_holder['error'])}")

        downloaded_files = list(Path(task_dir).glob("*"))
        if not downloaded_files:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail="Download completed but file not found")

        actual_file = downloaded_files[0]
        file_size_mb = round(actual_file.stat().st_size / (1024 * 1024), 2)

        if file_size_mb > MAX_FILE_SIZE_MB:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise HTTPException(status_code=413, detail=f"File size {file_size_mb}MB exceeds limit {MAX_FILE_SIZE_MB}MB")

        return {
            "success": True,
            "task_id": task_id,
            "title": title,
            "platform": platform,
            "author": author,
            "file_name": actual_file.name,
            "file_size_mb": file_size_mb,
            "download_url": f"/api/file/{task_id}/{actual_file.name}",
            "description": description,
            "duration": duration,
            "thumbnail": thumbnail,
        }

    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(task_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")


@app.get("/api/file/{task_id}/{file_name}", summary="Download file")
async def get_file(task_id: str, file_name: str):
    file_path = os.path.join(DOWNLOAD_DIR, task_id, file_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    return FileResponse(file_path, media_type="video/mp4", filename=file_name)


@app.delete("/api/file/{task_id}", summary="Delete file")
async def delete_file(task_id: str):
    task_dir = os.path.join(DOWNLOAD_DIR, task_id)
    if os.path.exists(task_dir):
        shutil.rmtree(task_dir, ignore_errors=True)
        return {"success": True, "message": "File deleted"}
    raise HTTPException(status_code=404, detail="Task not found")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
