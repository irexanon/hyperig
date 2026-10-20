"""Download Instagram CDN files."""
import requests


class MediaError(Exception):
    """A user-facing problem with a media file."""


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
