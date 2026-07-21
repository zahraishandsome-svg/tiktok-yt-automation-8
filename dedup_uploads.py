#!/usr/bin/env python3
"""
One-time dedup — runs on GitHub Actions (fast full TikTok fetch on a datacenter IP).

Marks the TikTok sources of the newest N YouTube uploads as already-posted so the
bot (esp. the popular slot) never reposts a video that's already on the channel.
Matches by caption: exact, then prefix (YouTube titles are truncated captions),
against BOTH the TikTok title and description.

The workflow commits the updated channel DB afterwards.

Usage: python dedup_uploads.py --channel channel_7 --yt-playlist UU... --newest 27
"""
import argparse
import logging
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import yt_dlp                                            # noqa: E402
from src.config import load_config                       # noqa: E402
from src.db import init_db, get_connection               # noqa: E402
from src.tiktok_downloader import get_profile_videos     # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("dedup")


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


ap = argparse.ArgumentParser()
ap.add_argument("--channel", required=True)
ap.add_argument("--yt-playlist", required=True, help="YouTube uploads playlist id (UU...)")
ap.add_argument("--newest", type=int, default=27)
args = ap.parse_args()

cfg = load_config()
ch = next(c for c in cfg["channels"] if c["id"] == args.channel)

# 1) Newest N YouTube uploads (these are the ones sourced from this TikTok)
opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
        "skip_download": True, "playlistend": args.newest}
with yt_dlp.YoutubeDL(opts) as y:
    yti = y.extract_info(f"https://www.youtube.com/playlist?list={args.yt_playlist}", download=False)
yt = [(norm(e.get("title")), e.get("id"), e.get("title") or "")
      for e in (yti.get("entries") or []) if norm(e.get("title"))]
log.info("YouTube uploads considered: %d", len(yt))

# 2) Full TikTok catalog (fast on GitHub's IP)
tiktoks = get_profile_videos(ch["tiktok_username"], end=None)
if not tiktoks:
    log.error("TikTok fetch failed or empty")
    sys.exit(1)
log.info("TikToks fetched: %d", len(tiktoks))
tt = [(norm(v.get("title")), norm(v.get("description")), v) for v in tiktoks]


def find(ynorm):
    hits, cut = [], ynorm.rsplit(" ", 1)[0]
    for t_title, t_desc, v in tt:
        if not t_title and not t_desc:
            continue
        if ynorm in (t_title, t_desc):
            hits.append(v); continue
        if len(ynorm) >= 12 and (t_title.startswith(ynorm) or t_desc.startswith(ynorm)):
            hits.append(v); continue
        if len(cut) >= 15 and (t_title.startswith(cut) or t_desc.startswith(cut)):
            hits.append(v)
    return hits


init_db()
now = datetime.utcnow().isoformat()
conn = get_connection()
marked, unmatched = set(), []
with conn:
    for ynorm, ytid, ytitle in yt:
        hits = find(ynorm)
        if hits:
            for v in hits:
                conn.execute(
                    "INSERT INTO posted_videos (channel_id, tiktok_video_id, format_type, youtube_video_id, posted_at, status, created_at, updated_at) "
                    "VALUES (?, ?, 'short', ?, ?, 'uploaded', ?, ?) "
                    "ON CONFLICT(channel_id, tiktok_video_id, format_type) DO UPDATE SET "
                    "status='uploaded', youtube_video_id=excluded.youtube_video_id, updated_at=excluded.updated_at",
                    (args.channel, v["id"], ytid, now, now, now))
                marked.add(v["id"])
        else:
            unmatched.append((ytid, ytitle))
conn.close()

log.info("MATCHED %d/%d YouTube uploads | TikToks marked posted: %d | unmatched: %d",
         len(yt) - len(unmatched), len(yt), len(marked), len(unmatched))
for ytid, t in unmatched:
    log.info("  UNMATCHED: %s | %s", ytid, t[:70])
