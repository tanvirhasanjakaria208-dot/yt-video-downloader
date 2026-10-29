import glob
import os
import re
import shutil
import tempfile
import threading
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file

app = Flask(__name__, static_folder="static", static_url_path="")

MAX_DURATION = int(os.environ.get("MAX_DURATION", 3600))  # seconds
MAX_JOBS = int(os.environ.get("MAX_JOBS", 2))
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
            "music.youtube.com", "youtu.be", "www.youtu.be"}

COOKIE_FILE = None
if os.environ.get("YT_COOKIES"):  # optional: paste cookies.txt contents here
    COOKIE_FILE = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    with open(COOKIE_FILE, "w") as f:
        f.write(os.environ["YT_COOKIES"])

slots = threading.BoundedSemaphore(MAX_JOBS)

FORMATS = {
    "hd": "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
    "sd": "bv*[height<=480][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=480]+ba/b[height<=480]/b",
    "mp3": "ba/b",
}


def base_opts():
    o = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if COOKIE_FILE:
        o["cookiefile"] = COOKIE_FILE
    return o


def valid_url(url):
    try:
        u = urlparse(url)
        return u.scheme in ("http", "https") and (u.hostname or "").lower() in YT_HOSTS
    except Exception:
        return False


def error_code(msg):
    m = msg.lower()
    if "sign in" in m or "confirm you" in m or "bot" in m:
        return "bot"
    if "private" in m or "unavailable" in m or "removed" in m:
        return "unavailable"
    if "age" in m and "restrict" in m:
        return "age"
    return "failed"


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/healthz")
def health():
    return "ok"


@app.post("/api/info")
def info():
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not valid_url(url):
        return jsonify(error="invalid"), 400
    try:
        with yt_dlp.YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
            d = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error=error_code(str(e))), 502

    if d.get("is_live"):
        return jsonify(error="live"), 400
    if (d.get("duration") or 0) > MAX_DURATION:
        return jsonify(error="toolong", max_minutes=MAX_DURATION // 60), 400

    heights = sorted({f["height"] for f in d.get("formats", [])
                      if f.get("height") and f.get("vcodec") not in (None, "none")})
    hd = max([h for h in heights if h <= 1080 and h >= 720], default=None)
    sd = max([h for h in heights if h <= 480], default=None)
    options = []
    if hd:
        options.append({"id": "hd", "height": hd})
    if sd:
        options.append({"id": "sd", "height": sd})
    options.append({"id": "mp3", "height": None})

    return jsonify(title=d.get("title"), thumbnail=d.get("thumbnail"),
                   duration=d.get("duration"), channel=d.get("uploader"),
                   options=options)


@app.get("/api/download")
def download():
    url = request.args.get("url", "").strip()
    q = request.args.get("q", "")
    token = re.sub(r"[^A-Za-z0-9]", "", request.args.get("t", ""))[:32]
    if not valid_url(url) or q not in FORMATS:
        return jsonify(error="invalid"), 400
    if not slots.acquire(blocking=False):
        return jsonify(error="busy"), 429

    tmp = tempfile.mkdtemp(prefix="ytdl_")

    def cleanup():
        shutil.rmtree(tmp, ignore_errors=True)
        slots.release()

    try:
        opts = {
            **base_opts(),
            "format": FORMATS[q],
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "match_filter": yt_dlp.utils.match_filter_func(
                f"duration <= {MAX_DURATION} & !is_live"),
        }
        if q == "mp3":
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                       "preferredcodec": "mp3",
                                       "preferredquality": "192"}]
        else:
            opts["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(opts) as ydl:
            d = ydl.extract_info(url, download=True)

        files = [f for f in glob.glob(os.path.join(tmp, "*"))
                 if not f.endswith((".part", ".ytdl", ".temp"))]
        if not files:
            cleanup()
            return jsonify(error="toolong"), 400
        path = max(files, key=os.path.getsize)
        ext = os.path.splitext(path)[1]
        title = re.sub(r'[\\/:*?"<>|\r\n]+', " ", d.get("title") or "video").strip()[:120] or "video"

        resp = send_file(path, as_attachment=True, download_name=f"{title}{ext}")
        if token:
            resp.set_cookie("dl_done", token, max_age=120, samesite="Lax")
        resp.call_on_close(cleanup)
        return resp
    except Exception as e:
        cleanup()
        return jsonify(error=error_code(str(e))), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
