"""Instagram Downloader, OSINT Profile Dump, Profile DB History, and Account Watcher utilities
built on instagrapi (https://subzeroid.github.io/instagrapi/latest/).
"""
import asyncio
import csv
import io
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from utils.db import sanitize_username
from utils.logger import get_logger

log = get_logger("osint")

IG_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/[^\s<>\"']+",
    re.IGNORECASE,
)


def extract_ig_url(text: str) -> str | None:
    """Return the first Instagram URL found in text, or None."""
    if not text:
        return None
    m = IG_URL_RE.search(text.strip())
    return m.group(0) if m else None


def detect_ig_url_category(url: str) -> tuple[str, str]:
    """Classify an Instagram URL into:
    ('reel', url), ('post', url), ('igtv', url), ('story', url),
    ('highlight', url), ('profile', username), or ('unknown', url).
    """
    parsed = urlparse(url.strip())
    path = (parsed.path or "").strip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "unknown", url

    first = parts[0].lower()
    if first in ("reel", "reels", "clips") and len(parts) >= 2:
        return "reel", url
    if first == "p" and len(parts) >= 2:
        return "post", url
    if first == "tv" and len(parts) >= 2:
        return "igtv", url
    if first == "s" or (first == "stories" and len(parts) >= 2 and parts[1].lower() == "highlights"):
        return "highlight", url
    if first == "stories" and len(parts) >= 2:
        return "story", url
    # Handle /username/p/CODE or /username/reel/CODE
    if len(parts) >= 3 and parts[1].lower() in ("p", "reel", "reels", "tv"):
        sub = parts[1].lower()
        return ("reel" if sub in ("reel", "reels") else "post"), url

    reserved = {
        "explore", "accounts", "direct", "about", "legal", "developer",
        "directory", "lite", "challenge", "ar", "emails", "session",
    }
    if first not in reserved and len(parts) == 1:
        uname = sanitize_username(parts[0])
        if uname:
            return "profile", uname
    return "unknown", url


def _safe_str(val) -> str:
    return str(val) if val is not None else ""


def _fetch_profile_sync(cl, username: str, deep_osint: bool = True) -> dict:
    """Blocking call executed via slot.ig._call: fetches user_info, user_about_v1, and user_friendship_v1."""
    uname = username.strip().lstrip("@")
    u = cl.user_info_by_username(uname)
    pk = int(u.pk)

    bio_links = []
    for b in getattr(u, "bio_links", None) or []:
        if isinstance(b, dict):
            bio_links.append(b.get("url") or b.get("lynx_url") or "")
        else:
            bio_links.append(getattr(b, "url", "") or "")
    bio_links = [x for x in bio_links if x]

    record = {
        "pk": str(pk),
        "username": _safe_str(u.username),
        "full_name": _safe_str(u.full_name),
        "biography": _safe_str(u.biography),
        "external_url": _safe_str(u.external_url),
        "bio_links": bio_links,
        "follower_count": int(getattr(u, "follower_count", 0) or 0),
        "following_count": int(getattr(u, "following_count", 0) or 0),
        "media_count": int(getattr(u, "media_count", 0) or 0),
        "is_private": bool(getattr(u, "is_private", False)),
        "is_verified": bool(getattr(u, "is_verified", False)),
        "is_business": bool(getattr(u, "is_business", False)),
        "category": _safe_str(getattr(u, "category", None) or getattr(u, "category_name", None) or ""),
        "public_email": _safe_str(getattr(u, "public_email", None) or ""),
        "public_phone_number": _safe_str(getattr(u, "public_phone_number", None) or ""),
        "city_name": _safe_str(getattr(u, "city_name", None) or ""),
        "profile_pic_url": _safe_str(getattr(u, "profile_pic_url_hd", None) or getattr(u, "profile_pic_url", None) or ""),
        "about": {},
        "friendship": {},
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }

    if deep_osint:
        try:
            about = cl.user_about_v1(pk)
            if isinstance(about, dict):
                record["about"] = about
        except Exception as e:
            log.debug("user_about_v1 failed for %s: %s", uname, e)

        try:
            rel = cl.user_friendship_v1(pk)
            if rel:
                record["friendship"] = {
                    "following": bool(getattr(rel, "following", False)),
                    "followed_by": bool(getattr(rel, "followed_by", False)),
                    "blocking": bool(getattr(rel, "blocking", False)),
                    "muting": bool(getattr(rel, "muting", False)),
                    "is_private": bool(getattr(rel, "is_private", False)),
                    "incoming_request": bool(getattr(rel, "incoming_request", False)),
                    "outgoing_request": bool(getattr(rel, "outgoing_request", False)),
                    "is_bestie": bool(getattr(rel, "is_bestie", False)),
                    "is_restricted": bool(getattr(rel, "is_restricted", False)),
                }
        except Exception as e:
            log.debug("user_friendship_v1 failed for %s: %s", uname, e)

    return record


async def fetch_and_save_profile_db(ws, slot, username: str, deep_osint: bool = True) -> tuple[dict, list[str], dict]:
    """Fetches profile + OSINT data from Instagram, stores/updates it in ws.store ('profile_db'),
    and returns (current_record, diff_lines, full_db_entry).
    """
    current = await slot.ig._call(_fetch_profile_sync, slot.ig.cl, username, deep_osint)
    pk = current["pk"]

    db_map: dict = await ws.store.get("profile_db", {}) or {}
    entry = db_map.get(pk) or {
        "pk": pk,
        "first_seen": current["scanned_at"],
        "scan_count": 0,
        "latest": None,
        "history": [],
    }

    diffs: list[str] = []
    prev = entry.get("latest")
    if isinstance(prev, dict):
        if prev.get("username") and prev.get("username") != current["username"]:
            diffs.append(f"• Username: <code>@{prev['username']}</code> → <code>@{current['username']}</code>")
        if prev.get("full_name") != current["full_name"]:
            diffs.append(f"• Name: <code>{prev.get('full_name') or '—'}</code> → <code>{current['full_name'] or '—'}</code>")
        if prev.get("biography") != current["biography"]:
            diffs.append("• Bio updated")
        if prev.get("external_url") != current["external_url"]:
            diffs.append(f"• Link: <code>{prev.get('external_url') or '—'}</code> → <code>{current['external_url'] or '—'}</code>")
        if prev.get("is_private") != current["is_private"]:
            state_str = "🔒 Private" if current["is_private"] else "🌐 Public"
            diffs.append(f"• Privacy changed to <b>{state_str}</b>")
        f_diff = current["follower_count"] - int(prev.get("follower_count", 0) or 0)
        if f_diff != 0:
            sign = f"+{f_diff}" if f_diff > 0 else str(f_diff)
            diffs.append(f"• Followers: <b>{sign}</b> (was {prev.get('follower_count', 0):,})")
        fg_diff = current["following_count"] - int(prev.get("following_count", 0) or 0)
        if fg_diff != 0:
            sign = f"+{fg_diff}" if fg_diff > 0 else str(fg_diff)
            diffs.append(f"• Following: <b>{sign}</b> (was {prev.get('following_count', 0):,})")
        m_diff = current["media_count"] - int(prev.get("media_count", 0) or 0)
        if m_diff != 0:
            sign = f"+{m_diff}" if m_diff > 0 else str(m_diff)
            diffs.append(f"• Posts: <b>{sign}</b> (was {prev.get('media_count', 0):,})")

    entry["scan_count"] = int(entry.get("scan_count", 0)) + 1
    entry["last_seen"] = current["scanned_at"]
    entry["latest"] = current

    hist = list(entry.get("history") or [])
    hist.append(
        {
            "scanned_at": current["scanned_at"],
            "username": current["username"],
            "full_name": current["full_name"],
            "biography": current["biography"],
            "follower_count": current["follower_count"],
            "following_count": current["following_count"],
            "media_count": current["media_count"],
            "is_private": current["is_private"],
            "diffs": diffs,
        }
    )
    entry["history"] = hist[-25:]  # keep last 25 snapshots per profile
    db_map[pk] = entry
    await ws.store.set("profile_db", db_map)

    return current, diffs, entry


def format_profile_card_html(rec: dict, diffs: list[str], db_entry: dict, is_watched: bool = False) -> str:
    """Format an HTML card for Telegram displaying Profile Info, OSINT data, and DB history."""
    badges = []
    if rec.get("is_verified"):
        badges.append("☑️ Verified")
    if rec.get("is_private"):
        badges.append("🔒 Private")
    else:
        badges.append("🌐 Public")
    if rec.get("is_business"):
        badges.append("💼 Business")
    if is_watched:
        badges.append("👁 Watched")

    badge_line = " · ".join(badges)
    lines = [
        f"🕵️ <b>@{rec['username']}</b> — <code>{rec['pk']}</code>",
        f"<i>{badge_line}</i>",
        "",
    ]
    if rec.get("full_name"):
        lines.append(f"👤 <b>Name:</b> {rec['full_name']}")
    if rec.get("category"):
        lines.append(f"🏷 <b>Category:</b> {rec['category']}")
    lines.append(
        f"📊 <b>Followers:</b> {rec['follower_count']:,} · "
        f"<b>Following:</b> {rec['following_count']:,} · "
        f"<b>Posts:</b> {rec['media_count']:,}"
    )

    if rec.get("biography"):
        bio_clean = (
            rec["biography"]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        lines.append(f"\n📝 <b>Bio:</b>\n{bio_clean}")

    links = []
    if rec.get("external_url"):
        links.append(rec["external_url"])
    for bl in rec.get("bio_links") or []:
        if bl not in links:
            links.append(bl)
    if links:
        lines.append("🔗 <b>Links:</b> " + ", ".join(f"<code>{l}</code>" for l in links[:3]))

    if rec.get("public_email") or rec.get("public_phone_number") or rec.get("city_name"):
        contact_parts = []
        if rec.get("public_email"):
            contact_parts.append(f"✉️ <code>{rec['public_email']}</code>")
        if rec.get("public_phone_number"):
            contact_parts.append(f"📞 <code>{rec['public_phone_number']}</code>")
        if rec.get("city_name"):
            contact_parts.append(f"📍 {rec['city_name']}")
        lines.append("📇 <b>Contact:</b> " + " · ".join(contact_parts))

    # OSINT section
    about = rec.get("about") or {}
    rel = rec.get("friendship") or {}
    osint_lines = []
    if about.get("date_joined"):
        osint_lines.append(f"📅 Joined: <b>{about['date_joined']}</b>")
    if about.get("country"):
        osint_lines.append(f"🌍 Country: <b>{about['country']}</b>")
    if about.get("former_usernames") is not None and str(about.get("former_usernames")) != "":
        osint_lines.append(f"🔄 Former Usernames: <b>{about['former_usernames']}</b>")

    if rel:
        rel_tags = []
        if rel.get("followed_by") and rel.get("following"):
            rel_tags.append("🤝 Mutual Follow")
        elif rel.get("followed_by"):
            rel_tags.append("⬅️ Follows You")
        elif rel.get("following"):
            rel_tags.append("➡️ You Follow")
        else:
            rel_tags.append("⚪ No Follow")
        if rel.get("is_bestie"):
            rel_tags.append("💚 Close Friend")
        if rel.get("blocking"):
            rel_tags.append("🚫 Blocked")
        if rel.get("incoming_request"):
            rel_tags.append("📩 Requested You")
        if rel.get("outgoing_request"):
            rel_tags.append("⏳ Requested")
        osint_lines.append("🔗 Relationship: " + ", ".join(rel_tags))

    if osint_lines:
        lines.append("\n🔎 <b>OSINT &amp; Metadata:</b>")
        for ol in osint_lines:
            lines.append(f"  {ol}")

    # Database History section
    scans = db_entry.get("scan_count", 1)
    first_seen = (db_entry.get("first_seen") or "")[:16].replace("T", " ")
    lines.append(f"\n🗄 <b>Profile DB:</b> Scan #{scans} (First saved: <code>{first_seen} UTC</code>)")
    if diffs:
        lines.append("📈 <b>Changes Since Last Scan:</b>")
        lines.extend(f"  {d}" for d in diffs)

    return "\n".join(lines)


# --------------------------------------------------------- Media & Link Downloader
def _download_url_sync(cl, url: str, tmp_dir: Path) -> dict:
    """Blocking download of an Instagram link into tmp_dir.
    Returns {"category": str, "caption": str, "author": str, "files": [Path, ...], "audio_url": str}.
    """
    cat, target = detect_ig_url_category(url)

    if cat == "story":
        try:
            spk = cl.story_pk_from_url(url)
            s_info = cl.story_info(spk)
            path = cl.story_download(spk, folder=tmp_dir)
            author = getattr(getattr(s_info, "user", None), "username", "") or ""
            return {
                "category": "Story",
                "caption": f"📖 Story by @{author}" if author else "📖 Instagram Story",
                "author": author,
                "files": [Path(path)],
            }
        except Exception:
            # Maybe /stories/username/ without story pk -> download active stories
            parsed = urlparse(url)
            parts = [p for p in (parsed.path or "").strip("/").split("/") if p]
            if len(parts) >= 2:
                uname = sanitize_username(parts[1])
                u = cl.user_info_by_username(uname)
                stories = cl.user_stories(int(u.pk))
                files = []
                for st in stories[:15]:
                    p = cl.story_download(st.pk, folder=tmp_dir)
                    files.append(Path(p))
                return {
                    "category": "Stories",
                    "caption": f"📖 {len(files)} Active Stories from @{u.username}",
                    "author": u.username,
                    "files": files,
                }
            raise

    if cat == "highlight":
        hpk = cl.highlight_pk_from_url(url)
        hinfo = cl.highlight_info(hpk)
        author = getattr(getattr(hinfo, "user", None), "username", "") or ""
        title = getattr(hinfo, "title", "Highlight") or "Highlight"
        files = []
        for item in (getattr(hinfo, "items", None) or [])[:20]:
            p = cl.story_download(item.pk, folder=tmp_dir)
            files.append(Path(p))
        return {
            "category": "Highlight",
            "caption": f"✨ Highlight '{title}' by @{author} ({len(files)} items)",
            "author": author,
            "files": files,
        }

    # Default: Post / Reel / IGTV / Carousel via media_pk_from_url
    mpk = cl.media_pk_from_url(url)
    info = cl.media_info(mpk)
    mtype = int(getattr(info, "media_type", 1) or 1)
    ptype = str(getattr(info, "product_type", "") or "")
    author = getattr(getattr(info, "user", None), "username", "") or ""
    caption_raw = str(getattr(info, "caption_text", "") or "").strip()
    likes = int(getattr(info, "like_count", 0) or 0)
    comments = int(getattr(info, "comment_count", 0) or 0)
    plays = int(getattr(info, "play_count", 0) or getattr(info, "view_count", 0) or 0)

    files: list[Path] = []
    if mtype == 1:
        label = "Photo"
        p = cl.photo_download(mpk, folder=tmp_dir)
        files.append(Path(p))
    elif mtype == 2:
        if ptype == "clips" or cat == "reel":
            label = "Reel"
            p = cl.clip_download(mpk, folder=tmp_dir)
        elif ptype == "igtv" or cat == "igtv":
            label = "IGTV"
            p = cl.igtv_download(mpk, folder=tmp_dir)
        else:
            label = "Video"
            p = cl.video_download(mpk, folder=tmp_dir)
        files.append(Path(p))
    elif mtype == 8:
        label = "Carousel Album"
        paths = cl.album_download(mpk, folder=tmp_dir)
        files.extend(Path(x) for x in paths)
    else:
        label = "Media"
        p = cl.photo_download(mpk, folder=tmp_dir)
        files.append(Path(p))

    stats = f"❤️ {likes:,} · 💬 {comments:,}"
    if plays:
        stats += f" · ▶️ {plays:,}"
    cap_preview = (caption_raw[:400] + "…") if len(caption_raw) > 400 else caption_raw
    header = f"📥 <b>{label}</b> by <b>@{author}</b>\n{stats}"
    if cap_preview:
        safe_cap = cap_preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        header += f"\n\n{safe_cap}"

    return {
        "category": label,
        "caption": header,
        "author": author,
        "files": files,
    }


async def download_ig_link(slot, url: str, tmp_dir: Path) -> dict:
    """Async wrapper for downloading any Instagram link into tmp_dir."""
    return await slot.ig._call(_download_url_sync, slot.ig.cl, url, tmp_dir)


# --------------------------------------------------------- Dump Actions (Avatar, Stories, Highlights, Posts, Reels, Tagged, Followers)
def _download_avatar_sync(cl, username: str, tmp_dir: Path) -> tuple[Path, str]:
    u = cl.user_info_by_username(username.strip().lstrip("@"))
    pic_url = str(getattr(u, "profile_pic_url_hd", None) or getattr(u, "profile_pic_url", None) or "")
    if not pic_url:
        raise RuntimeError(f"No profile picture URL found for @{u.username}")
    path = cl.photo_download_by_url(pic_url, filename=f"{u.username}_avatar", folder=tmp_dir)
    return Path(path), u.username


def _download_active_stories_sync(cl, username: str, tmp_dir: Path) -> tuple[list[Path], str]:
    u = cl.user_info_by_username(username.strip().lstrip("@"))
    stories = cl.user_stories(int(u.pk))
    files: list[Path] = []
    for st in stories:
        p = cl.story_download(st.pk, folder=tmp_dir)
        files.append(Path(p))
    return files, u.username


def _list_highlights_sync(cl, username: str) -> tuple[list[dict], str]:
    u = cl.user_info_by_username(username.strip().lstrip("@"))
    hl_list = cl.user_highlights(int(u.pk))
    out = []
    for h in hl_list:
        out.append(
            {
                "pk": str(h.pk),
                "title": str(getattr(h, "title", "Highlight") or "Highlight"),
                "media_count": int(getattr(h, "media_count", 0) or len(getattr(h, "items", []) or [])),
            }
        )
    return out, u.username


def _download_highlight_sync(cl, highlight_pk: str, tmp_dir: Path) -> tuple[list[Path], str]:
    hinfo = cl.highlight_info(highlight_pk)
    title = str(getattr(hinfo, "title", "Highlight") or "Highlight")
    files: list[Path] = []
    for item in getattr(hinfo, "items", None) or []:
        p = cl.story_download(item.pk, folder=tmp_dir)
        files.append(Path(p))
    return files, title


def _download_media_item_sync(cl, m, tmp_dir: Path) -> list[Path]:
    mtype = int(getattr(m, "media_type", 1) or 1)
    ptype = str(getattr(m, "product_type", "") or "")
    files: list[Path] = []
    if mtype == 1:
        p = cl.photo_download(m.pk, folder=tmp_dir)
        files.append(Path(p))
    elif mtype == 2:
        if ptype == "clips":
            p = cl.clip_download(m.pk, folder=tmp_dir)
        elif ptype == "igtv":
            p = cl.igtv_download(m.pk, folder=tmp_dir)
        else:
            p = cl.video_download(m.pk, folder=tmp_dir)
        files.append(Path(p))
    elif mtype == 8:
        paths = cl.album_download(m.pk, folder=tmp_dir)
        files.extend(Path(x) for x in paths)
    return files


def _fetch_user_media_list_sync(cl, username: str, kind: str, amount: int) -> tuple[list, str]:
    """kind: 'posts' | 'reels' | 'tagged'. amount=0 means ALL (capped at 100 per batch for safety)."""
    u = cl.user_info_by_username(username.strip().lstrip("@"))
    pk = int(u.pk)
    target_amount = amount if amount > 0 else 100  # 0 = All (up to 100 items in one Telegram dump run)
    if kind == "reels":
        items = cl.user_clips(pk, amount=target_amount)
    elif kind == "tagged":
        items = cl.usertag_medias(pk, amount=target_amount)
    else:
        items = cl.user_medias(pk, amount=target_amount)
    return list(items or []), u.username


def _export_follow_list_sync(cl, username: str, kind: str, amount: int) -> tuple[bytes, bytes, int, str]:
    """kind: 'followers' | 'following'. amount=0 means ALL (0 in instagrapi user_followers_v1 means all).
    Returns (csv_bytes, json_bytes, count, canonical_username).
    """
    u = cl.user_info_by_username(username.strip().lstrip("@"))
    pk = int(u.pk)
    fetch_amt = amount if amount > 0 else 0  # instagrapi: amount=0 fetches all!
    if kind == "followers":
        users = cl.user_followers_v1(pk, amount=fetch_amt)
    else:
        users = cl.user_following_v1(pk, amount=fetch_amt)

    rows = []
    for item in users or []:
        rows.append(
            {
                "pk": str(getattr(item, "pk", "")),
                "username": str(getattr(item, "username", "")),
                "full_name": str(getattr(item, "full_name", "")),
                "is_private": bool(getattr(item, "is_private", False)),
                "is_verified": bool(getattr(item, "is_verified", False)),
                "profile_pic_url": str(getattr(item, "profile_pic_url", "")),
            }
        )

    csv_buf = io.StringIO()
    writer = csv.DictWriter(
        csv_buf,
        fieldnames=["pk", "username", "full_name", "is_private", "is_verified", "profile_pic_url"],
    )
    writer.writeheader()
    writer.writerows(rows)

    csv_bytes = csv_buf.getvalue().encode("utf-8")
    json_bytes = json.dumps(rows, indent=2, ensure_ascii=False).encode("utf-8")
    return csv_bytes, json_bytes, len(rows), u.username


# --------------------------------------------------------- OSINT & Story/Post Watcher
async def get_watched_targets(ws) -> dict[str, dict]:
    """Return map of sanitized_username -> watcher metadata stored in ws.store."""
    data = await ws.store.get("watched_profiles", {}) or {}
    return data if isinstance(data, dict) else {}


async def is_target_watched(ws, username: str) -> bool:
    targets = await get_watched_targets(ws)
    return sanitize_username(username) in targets


async def toggle_watch_target(ws, username: str, pk: str = "") -> bool:
    """Toggle watch status for `username`. Returns True if now watched, False if removed."""
    uname = sanitize_username(username)
    if not uname:
        return False
    targets = await get_watched_targets(ws)
    if uname in targets:
        targets.pop(uname, None)
        await ws.store.set("watched_profiles", targets)
        return False
    targets[uname] = {
        "username": uname,
        "pk": str(pk or ""),
        "added_at": datetime.now(timezone.utc).isoformat(),
        "last_checked": "",
        "seen_stories": [],
        "seen_medias": [],
        "initialized": False,
    }
    await ws.store.set("watched_profiles", targets)
    return True


def _scan_target_activity_sync(cl, username: str) -> tuple[list[str], list[str]]:
    """Fetch current story PKs and recent media PKs for watcher comparison."""
    u = cl.user_info_by_username(username.strip().lstrip("@"))
    pk = int(u.pk)
    story_pks: list[str] = []
    media_pks: list[str] = []
    try:
        stories = cl.user_stories(pk) or []
        story_pks = [str(s.pk) for s in stories]
    except Exception:
        pass
    try:
        medias = cl.user_medias(pk, amount=5) or []
        media_pks = [str(m.pk) for m in medias]
    except Exception:
        pass
    return story_pks, media_pks


async def check_watched_target(ws, slot, username: str, tmp_dir: Path) -> dict:
    """Check a watched Instagram account for profile changes, new stories, and new posts.
    Returns {"username": str, "diffs": list[str], "new_story_files": list[Path], "new_post_files": list[Path]}.
    """
    uname = sanitize_username(username)
    rec, diffs, _ = await fetch_and_save_profile_db(ws, slot, uname, deep_osint=False)
    canonical = sanitize_username(rec.get("username") or uname)

    targets = await get_watched_targets(ws)
    w_meta = targets.get(uname) or targets.get(canonical) or {
        "username": canonical,
        "pk": rec["pk"],
        "added_at": datetime.now(timezone.utc).isoformat(),
        "seen_stories": [],
        "seen_medias": [],
        "initialized": False,
    }

    story_pks, media_pks = await slot.ig._call(_scan_target_activity_sync, slot.ig.cl, canonical)
    prev_stories = set(w_meta.get("seen_stories") or [])
    prev_medias = set(w_meta.get("seen_medias") or [])
    initialized = bool(w_meta.get("initialized"))

    new_story_files: list[Path] = []
    new_post_files: list[Path] = []

    if initialized:
        new_s_pks = [spk for spk in story_pks if spk not in prev_stories]
        for spk in new_s_pks[:5]:
            try:
                p = await slot.ig._call(slot.ig.cl.story_download, int(spk), None, tmp_dir)
                new_story_files.append(Path(p))
            except Exception as e:
                log.debug("Watcher story download failed for %s: %s", spk, e)

        new_m_pks = [mpk for mpk in media_pks if mpk not in prev_medias]
        for mpk in new_m_pks[:3]:
            try:
                minfo = await slot.ig._call(slot.ig.cl.media_info, int(mpk))
                files = await slot.ig._call(_download_media_item_sync, slot.ig.cl, minfo, tmp_dir)
                new_post_files.extend(files)
            except Exception as e:
                log.debug("Watcher media download failed for %s: %s", mpk, e)

    w_meta["username"] = canonical
    w_meta["pk"] = rec["pk"]
    w_meta["last_checked"] = datetime.now(timezone.utc).isoformat()
    w_meta["seen_stories"] = list(dict.fromkeys(story_pks + list(prev_stories)))[:100]
    w_meta["seen_medias"] = list(dict.fromkeys(media_pks + list(prev_medias)))[:100]
    w_meta["initialized"] = True

    if uname != canonical:
        targets.pop(uname, None)
    targets[canonical] = w_meta
    await ws.store.set("watched_profiles", targets)

    return {
        "username": canonical,
        "diffs": diffs if initialized else [],
        "new_story_files": new_story_files,
        "new_post_files": new_post_files,
    }

