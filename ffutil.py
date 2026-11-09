"""ffmpeg discovery + probe/run helpers.

Every tool in Shorts Factory shells out to ffmpeg/ffprobe. This module finds
the binaries once (PATH first, then the usual suspects) and provides the probe
helpers the engines build on. The duration helpers read the VIDEO STREAM, never
the format duration — a still without -loop 1 hides inside format duration
(concat trap 4).
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

_FFMPEG = None
_FFPROBE = None

_CANDIDATE_DIRS = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    str(Path.home() / ".hermes" / "tools"),  # hermes-bundled builds (drawtext!)
]

INSTALL_HINT = (
    "ffmpeg not found. Install it: macOS `brew install ffmpeg` · "
    "Debian/Ubuntu `sudo apt install ffmpeg` · Windows `winget install Gyan.FFmpeg`. "
    "Note: builds WITHOUT libfreetype lack drawtext — Shorts Factory does not need "
    "drawtext (captions render as PNGs via Pillow), so any build works."
)


def _search(name: str):
    found = shutil.which(name)
    if found:
        return found
    for d in _CANDIDATE_DIRS:
        p = Path(d)
        if not p.is_dir():
            continue
        # hermes tools dir nests versioned subdirs: ffmpeg-9.0.1-darwin-arm64/ffmpeg
        direct = p / name
        if direct.is_file() and os.access(direct, os.X_OK):
            return str(direct)
        try:
            for sub in sorted(p.iterdir(), reverse=True):
                cand = sub / name
                if sub.is_dir() and cand.is_file() and os.access(cand, os.X_OK):
                    return str(cand)
        except OSError:
            continue
    return None


def ffmpeg():
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = _search("ffmpeg")
    return _FFMPEG


def ffprobe():
    global _FFPROBE
    if _FFPROBE is None:
        _FFPROBE = _search("ffprobe") or shutil.which("ffprobe")
    return _FFPROBE


def binaries() -> dict:
    out: dict = {"ffmpeg": ffmpeg(), "ffprobe": ffprobe()}
    for key, path in list(out.items()):
        if path:
            try:
                r = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=15)
                m = re.search(r"(\d+\.\d+(?:\.\d+)?)", r.stdout.split("\n")[0])
                out[key + "_version"] = m.group(1) if m else "unknown"
            except Exception:
                out[key + "_version"] = "error"
        else:
            out[key + "_version"] = None
    out["ready"] = bool(out["ffmpeg"] and out["ffprobe"])
    if not out["ready"]:
        out["hint"] = INSTALL_HINT
    return out


def run(cmd: list, timeout: int = 900) -> tuple:
    """Run a command; returns (returncode, stdout, stderr)."""
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def probe(path: str) -> dict:
    rc, out, err = run([
        ffprobe() or "ffprobe", "-v", "error",
        "-show_format", "-show_streams",
        "-of", "json", str(path),
    ], timeout=60)
    if rc != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {err.strip()[:300]}")
    return json.loads(out)


def stream_duration(path: str, kind: str = "v") -> float:
    """Duration of the VIDEO (v) or AUDIO (a) stream — never the format."""
    d = probe(path)
    for s in d.get("streams", []):
        if s.get("codec_type") == ("video" if kind == "v" else "audio"):
            dur = s.get("duration")
            if dur:
                return float(dur)
    # fall back to format only when the stream carries no duration
    return float(d.get("format", {}).get("duration", 0.0))


def video_info(path: str) -> dict:
    d = probe(path)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), {})
    fps = 30.0
    r = v.get("r_frame_rate", "")
    if r and "/" in r:
        num, den = r.split("/")
        if float(den or 1) > 0:
            fps = float(num) / float(den)
    return {
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "fps": round(fps, 3),
        "duration": stream_duration(path, "v"),
        "has_audio": bool(a),
        "audio_rate": int(a.get("sample_rate") or 0),
        "audio_channels": int(a.get("channels") or 0),
        "codec": v.get("codec_name"),
        "pix_fmt": v.get("pix_fmt"),
        "color_space": v.get("color_space"),
        "sar": v.get("sample_aspect_ratio"),
    }


def is_still(path: str) -> bool:
    return Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
