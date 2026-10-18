"""Media helpers: download Instagram CDN files, convert Telegram files into what Instagram accepts.

Instagram direct messages accept exactly three kinds of upload through instagrapi:
  * photo  -> JPEG
  * video  -> MP4 (H.264 + AAC)
  * voice  -> M4A (AAC)          (instagrapi: "the server rejects other formats")
Telegram sends voice notes as OGG/Opus, videos as MP4/WebM/HEVC, stickers as WEBP, so everything
goes through ffmpeg (audio/video) or Pillow (images) first.
"""
import shutil
import subprocess
from pathlib import Path

import requests

from utils.logger import get_logger

log = get_logger("media")


class MediaError(Exception):
    """A user-facing problem with a media file."""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def download(url: str, max_bytes: int, proxy: str | None = None, verify: bool | str = True) -> bytes:
    """Fetch a (signed) Instagram CDN url. Streams, and refuses anything over max_bytes."""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    with requests.get(url, stream=True, timeout=(10, 60), proxies=proxies, verify=verify) as r:
        r.raise_for_status()
        declared = int(r.headers.get("Content-Length") or 0)
        if declared > max_bytes:
            raise MediaError(f"file is {declared // 1_000_000} MB, over the {max_bytes // 1_000_000} MB limit")
        buf = bytearray()
        for chunk in r.iter_content(64 * 1024):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise MediaError(f"file is over the {max_bytes // 1_000_000} MB limit")
    if not buf:
        raise MediaError("the download was empty")
    return bytes(buf)


def _ffmpeg(args: list[str], src: Path, dst: Path, timeout: int = 180) -> Path:
    if not ffmpeg_available():
        raise MediaError("ffmpeg is not installed on the server (needed to convert media for Instagram)")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), *args, str(dst)]
    try:
        run = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MediaError("converting the file took too long") from None
    if run.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        log.warning("ffmpeg failed: %s", run.stderr.decode(errors="replace")[-400:])
        raise MediaError("could not convert that file for Instagram")
    return dst


def to_ig_voice(src: Path, dst: Path) -> Path:
    """Any audio -> AAC in an MP4 container (.m4a), mono, which is what Instagram voice notes are."""
    return _ffmpeg(["-vn", "-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "1", "-movflags", "+faststart"], src, dst)


def to_ig_video(src: Path, dst: Path) -> Path:
    """Any video -> H.264/AAC MP4 with even dimensions (Instagram rejects odd sizes)."""
    return _ffmpeg(
        [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
        ],
        src,
        dst,
        timeout=300,
    )


def to_ig_photo(src: Path, dst: Path) -> Path:
    """Any still image (PNG, WEBP sticker, ...) -> JPEG."""
    from PIL import Image

    try:
        with Image.open(src) as im:
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
            im.save(dst, "JPEG", quality=92)
    except Exception:
        raise MediaError("could not read that image") from None
    return dst
