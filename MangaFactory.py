#!/usr/bin/env python3
"""
MangaFactory — combined MangaDexFactory + CBZ Factory, single-file edition.

Just run:  python MangaFactory.py

Tab 1 — Download:      Grab chapters from MangaDex, optionally package as CBZ.
Tab 2 — CBZ Processor: Take existing .cbz files, rename pages to
                       Chapter_XX_page_YYY.ext, insert a cover image
                       (000_cover.ext), and repackage as one volume CBZ
                       or a folder tree.

No pip install needed — dependencies are fetched automatically on first
run into a local folder (.mdf_libs/) next to this script.
"""

# ── Step 1: bootstrap dependencies before anything else imports ───────────────

import sys
import os
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIBS = os.path.join(_HERE, ".mdf_libs")
REQUIRED = {"flask": "flask>=3.0", "requests": "requests>=2.31"}

def _ensure_deps():
    missing = []
    for pkg, spec in REQUIRED.items():
        try:
            __import__(pkg)
        except ImportError:
            missing.append(spec)
    if missing:
        print("MangaFactory: installing dependencies (one-time setup)...")
        os.makedirs(_LIBS, exist_ok=True)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--target", _LIBS,
             "--quiet", "--disable-pip-version-check"] + missing
        )
        print("Done.\n")

_ensure_deps()

if _LIBS not in sys.path:
    sys.path.insert(0, _LIBS)

# ── Step 2: real imports ──────────────────────────────────────────────────────

import re
import time
import threading
import requests
import zipfile
import json
import queue
import shutil
import webbrowser
from flask import Flask, request, jsonify, Response

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
MANGADEX_API = "https://api.mangadex.org"
DOWNLOAD_BASE = os.path.expanduser("~/Downloads/manga")
download_sessions = {}   # MDF download sessions
cbz_sessions = {}        # CBZ processing sessions

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}

# ── Shared helpers ────────────────────────────────────────────────────────────

def slugify(text):
    if not text:
        return "unknown"
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '_', text)
    return text[:40]

def format_chapter_num(ch_num):
    if ch_num is None:
        return "00"
    try:
        f = float(ch_num)
        if f == int(f):
            return str(int(f)).zfill(2)
        whole = int(f)
        dec = str(ch_num).split('.')[-1]
        return f"{str(whole).zfill(2)}_{dec}"
    except:
        return str(ch_num).replace('.', '_')

def format_volume_num(vol_num):
    try:
        return str(int(float(vol_num))).zfill(2)
    except:
        return str(vol_num).zfill(2)

# ── MangaDex helpers ──────────────────────────────────────────────────────────

def get_manga_info(manga_id):
    r = requests.get(f"{MANGADEX_API}/manga/{manga_id}",
                     params={"includes[]": "author"}, timeout=10)
    r.raise_for_status()
    data = r.json()["data"]
    attrs = data["attributes"]
    title = (
        attrs["title"].get("en") or
        attrs["title"].get("ja-ro") or
        next(iter(attrs["title"].values()), "Unknown")
    )
    return {"id": manga_id, "title": title}

def get_all_chapters(manga_id):
    chapters = []
    offset = 0
    limit = 100
    while True:
        params = {
            "manga": manga_id,
            "translatedLanguage[]": "en",
            "order[chapter]": "asc",
            "limit": limit,
            "offset": offset,
            "includes[]": "scanlation_group",
        }
        r = requests.get(f"{MANGADEX_API}/chapter", params=params, timeout=10)
        r.raise_for_status()
        result = r.json()
        data = result["data"]
        if not data:
            break
        for ch in data:
            attrs = ch["attributes"]
            chapters.append({
                "id": ch["id"],
                "chapter": attrs.get("chapter"),
                "title": attrs.get("title") or "",
                "pages": attrs.get("pages", 0),
                "volume": attrs.get("volume") or "",
            })
        offset += limit
        if offset >= result["total"]:
            break
        time.sleep(0.3)
    return chapters

def detect_gaps(chapters):
    nums = []
    for ch in chapters:
        try:
            nums.append(float(ch["chapter"]))
        except:
            pass
    if not nums:
        return []
    nums_sorted = sorted(set(nums))
    gaps = []
    for i in range(len(nums_sorted) - 1):
        a, b = nums_sorted[i], nums_sorted[i + 1]
        if b - a > 1.0:
            gaps.append({"from": a, "to": b})
    return gaps

def deduplicate_chapters(chapters):
    seen = {}
    result = []
    for ch in chapters:
        key = ch["chapter"]
        if key not in seen:
            seen[key] = True
            result.append(ch)
    return result

def group_chapters_by_volume(chapters):
    groups = {}
    for ch in chapters:
        vol = (ch.get("volume") or "").strip()
        key = vol if vol else "unnumbered"
        groups.setdefault(key, []).append(ch)
    return groups

def extract_manga_id(url_or_id):
    url_or_id = url_or_id.strip()
    m = re.search(r'mangadex\.org/title/([a-f0-9-]{36})', url_or_id)
    if m:
        return m.group(1)
    m = re.match(
        r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$',
        url_or_id)
    if m:
        return url_or_id
    return None

def download_chapter_worker(session_id, chapter, series_slug, output_dir, q):
    ch_id = chapter["id"]
    ch_num = format_chapter_num(chapter["chapter"])
    prefix = f"{series_slug}_ch{ch_num}"
    try:
        r = requests.get(f"{MANGADEX_API}/at-home/server/{ch_id}", timeout=10)
        r.raise_for_status()
        data = r.json()
        base_url = data["baseUrl"]
        ch_hash = data["chapter"]["hash"]
        files = data["chapter"]["data"]
        total = len(files)
        q.put({"type": "chapter_start", "chapter": chapter["chapter"], "total": total})
        downloaded_files = []
        for i, fname in enumerate(files):
            ext = fname.rsplit('.', 1)[-1] if '.' in fname else 'jpg'
            page_num = str(i + 1).zfill(3)
            out_name = f"{prefix}_{page_num}.{ext}"
            out_path = os.path.join(output_dir, out_name)
            if os.path.exists(out_path):
                q.put({"type": "page_done", "page": i + 1, "total": total,
                       "file": out_name, "skipped": True})
                downloaded_files.append(out_path)
                continue
            img_url = f"{base_url}/data/{ch_hash}/{fname}"
            try:
                img_r = requests.get(img_url, timeout=20)
                img_r.raise_for_status()
                with open(out_path, 'wb') as f:
                    f.write(img_r.content)
                downloaded_files.append(out_path)
                q.put({"type": "page_done", "page": i + 1, "total": total,
                       "file": out_name, "skipped": False})
            except Exception as e:
                q.put({"type": "page_error", "page": i + 1, "error": str(e)})
            time.sleep(0.35)
        q.put({"type": "chapter_done", "chapter": chapter["chapter"],
               "files": downloaded_files})
    except Exception as e:
        q.put({"type": "chapter_error", "chapter": chapter["chapter"],
               "error": str(e)})

def build_cbz_worker(session_id, series_slug, completed_chapters, output_dir, q):
    vol_groups = {}
    for ch in completed_chapters:
        vol = (ch.get("volume") or "").strip()
        key = vol if vol else "unnumbered"
        vol_groups.setdefault(key, [])
        vol_groups[key].extend(ch.get("files", []))

    def vol_sort_key(v):
        try:
            return (0, float(v))
        except:
            return (1, v)

    sorted_vols = sorted(vol_groups.keys(), key=vol_sort_key)
    total_vols = len(sorted_vols)
    q.put({"type": "cbz_start", "total": total_vols})
    for i, vol_key in enumerate(sorted_vols):
        if download_sessions.get(session_id) is None:
            break
        files = sorted(vol_groups[vol_key])
        if not files:
            continue
        if vol_key == "unnumbered":
            cbz_name = f"{series_slug}_vol_unnumbered.cbz"
        else:
            cbz_name = f"{series_slug}_vol{format_volume_num(vol_key)}.cbz"
        cbz_path = os.path.join(output_dir, cbz_name)
        q.put({"type": "cbz_building", "vol": vol_key, "cbz": cbz_name,
               "file_count": len(files)})
        try:
            with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_STORED) as zf:
                for fp in files:
                    if os.path.exists(fp):
                        zf.write(fp, os.path.basename(fp))
            q.put({"type": "cbz_done", "vol": vol_key, "cbz": cbz_name,
                   "index": i + 1, "total": total_vols})
        except Exception as e:
            q.put({"type": "cbz_error", "vol": vol_key, "error": str(e)})
    q.put({"type": "all_done"})

# ── CBZ Processor helpers (ported from CBZ Factory JS) ────────────────────────

def cbz_detect_chapter_number(filename):
    """
    Port of CBZ Factory's extractChapterNumber(). Strategy:
      1) If a keyword like 'chapter', 'ch', 'c', or '#' is present, take the
         number next to it — up to 4 digits (covers long-runners like One Piece).
      2) Otherwise fall back to any 1–3 digit number in the filename, which
         avoids accidentally grabbing 4-digit years.
    """
    name = re.sub(r'\.cbz$', '', filename, flags=re.IGNORECASE)
    # Keyword-anchored patterns first — allow up to 4 digits when prefixed.
    patterns = [
        re.compile(r'(?:chapter|chap|ch|c)[\s._-]*(\d{1,4})', re.IGNORECASE),
        re.compile(r'#(\d{1,4})'),
    ]
    for p in patterns:
        m = p.search(name)
        if m:
            return m.group(1)
    # Fallback: any short number (cap at 3 digits to skip years).
    tokens = re.findall(r'\d+', name)
    candidates = [t for t in tokens if 1 <= len(t) <= 3]
    return candidates[-1] if candidates else ""

def cbz_sort_key(name):
    """Natural sort — splits a string into alternating int/str parts."""
    parts = re.split(r'(\d+)', name.lower())
    return [int(p) if p.isdigit() else p for p in parts]

def cbz_list_image_entries(zf):
    """Return sorted list of image entry names inside an open ZipFile."""
    entries = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        ext = info.filename.rsplit('.', 1)[-1].lower() if '.' in info.filename else ''
        if ext in IMAGE_EXTS:
            entries.append(info.filename)
    entries.sort(key=cbz_sort_key)
    return entries

def cbz_volume_folder_name(volume_value):
    v = (volume_value or "").strip()
    return f"Volume_{v}" if v else "New Volume"

def cbz_scan_folder(folder_path):
    """Scan a folder for .cbz files, return list sorted naturally with detected chapter numbers."""
    folder_path = os.path.expanduser(folder_path)
    if not os.path.isdir(folder_path):
        raise ValueError(f"Folder not found: {folder_path}")
    files = []
    for entry in sorted(os.listdir(folder_path), key=cbz_sort_key):
        full = os.path.join(folder_path, entry)
        if os.path.isfile(full) and entry.lower().endswith('.cbz'):
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            files.append({
                "path": full,
                "name": entry,
                "size": size,
                "detected_chapter": cbz_detect_chapter_number(entry),
            })
    return files

def cbz_process_worker(session_id, items, volume_value, cover_path,
                       output_dir, mode, q):
    """
    items: list of dicts with 'path' and 'chapter' (string chapter number)
    mode: 'cbz' (single .cbz output) or 'folder' (folder structure output)
    volume_value: volume string (e.g. '03')  → folder/CBZ named Volume_03
    cover_path: optional filesystem path to a cover image
    output_dir: where to write the final CBZ or folder
    """
    try:
        output_dir = os.path.expanduser(output_dir)
        os.makedirs(output_dir, exist_ok=True)
        vol_folder = cbz_volume_folder_name(volume_value)
        vol_fs     = vol_folder.replace(' ', '_')

        # Cover preparation
        cover_entry = None
        if cover_path:
            cover_path = os.path.expanduser(cover_path)
            if os.path.isfile(cover_path):
                ext = cover_path.rsplit('.', 1)[-1].lower() if '.' in cover_path else 'jpg'
                cover_entry = (f"000_cover.{ext}", cover_path)
            else:
                q.put({"type": "log", "level": "warn",
                       "text": f"Cover file not found: {cover_path} — skipping."})

        total_items = len(items)
        q.put({"type": "process_start", "total": total_items,
               "mode": mode, "volume": vol_folder})

        # Pre-compute total pages for the overall progress bar
        total_pages = 0
        item_info = []
        for item in items:
            try:
                with zipfile.ZipFile(item["path"], 'r') as zf:
                    images = cbz_list_image_entries(zf)
                    total_pages += len(images)
                    item_info.append((item, images))
            except Exception as e:
                q.put({"type": "file_error", "file": os.path.basename(item["path"]),
                       "error": str(e)})
                item_info.append((item, None))
        q.put({"type": "pages_total", "total": total_pages})

        pages_done = 0

        # Prepare output container
        if mode == "cbz":
            out_cbz_path = os.path.join(output_dir, f"{vol_fs}.cbz")
            out_zip = zipfile.ZipFile(out_cbz_path, 'w', zipfile.ZIP_STORED)
            q.put({"type": "log", "level": "info",
                   "text": f"Output CBZ: {out_cbz_path}"})
            if cover_entry:
                out_zip.write(cover_entry[1], cover_entry[0])
                q.put({"type": "log", "level": "ok",
                       "text": f"+ cover: {cover_entry[0]}"})
        else:
            # folder mode
            vol_dir = os.path.join(output_dir, vol_fs)
            os.makedirs(vol_dir, exist_ok=True)
            out_zip = None
            q.put({"type": "log", "level": "info",
                   "text": f"Output folder: {vol_dir}"})
            if cover_entry:
                dst = os.path.join(vol_dir, cover_entry[0])
                shutil.copy2(cover_entry[1], dst)
                q.put({"type": "log", "level": "ok",
                       "text": f"+ cover: {cover_entry[0]}"})

        try:
            for idx, (item, images) in enumerate(item_info):
                if cbz_sessions.get(session_id) is None:
                    q.put({"type": "log", "level": "warn", "text": "Cancelled."})
                    break
                if images is None:
                    continue
                file_name = os.path.basename(item["path"])
                ch_num = (item.get("chapter") or "").strip()
                if not ch_num:
                    q.put({"type": "file_error", "file": file_name,
                           "error": "Missing chapter number"})
                    continue

                base_name = f"Chapter_{ch_num}"
                q.put({"type": "file_start", "index": idx + 1, "total": total_items,
                       "file": file_name, "chapter": ch_num,
                       "page_count": len(images)})

                try:
                    with zipfile.ZipFile(item["path"], 'r') as src:
                        total = len(images)
                        pad_len = len(str(total))
                        for i, entry in enumerate(images):
                            if cbz_sessions.get(session_id) is None:
                                break
                            ext = entry.rsplit('.', 1)[-1].lower() if '.' in entry else 'jpg'
                            page_num = str(i + 1).zfill(pad_len)
                            new_name = f"{base_name}_page_{page_num}.{ext}"
                            data = src.read(entry)
                            if mode == "cbz":
                                out_zip.writestr(new_name, data)
                            else:
                                with open(os.path.join(vol_dir, new_name), 'wb') as f:
                                    f.write(data)
                            pages_done += 1
                            q.put({"type": "page_done", "file": new_name,
                                   "pages_done": pages_done, "pages_total": total_pages})
                    q.put({"type": "file_done", "file": file_name,
                           "chapter": ch_num, "pages": len(images)})
                except Exception as e:
                    q.put({"type": "file_error", "file": file_name,
                           "error": str(e)})
        finally:
            if out_zip is not None:
                out_zip.close()

        if mode == "cbz":
            q.put({"type": "all_done",
                   "output_path": os.path.join(output_dir, f"{vol_fs}.cbz"),
                   "mode": mode})
        else:
            q.put({"type": "all_done",
                   "output_path": os.path.join(output_dir, vol_fs),
                   "mode": mode})
    except Exception as e:
        q.put({"type": "fatal", "error": str(e)})
        q.put({"type": "all_done", "output_path": "", "mode": mode})

# ── Inline HTML ───────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MangaFactory</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0e0e12; --surface: #16161c; --surface2: #1e1e28; --border: #2a2a38;
    --accent: #e8441a; --accent2: #f7a23e; --text: #e8e8f0; --muted: #6a6a80;
    --success: #3ecf8e; --warn: #f7a23e; --danger: #e05252;
    --mono: 'JetBrains Mono', monospace; --sans: 'Syne', sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--sans); min-height: 100vh; overflow-x: hidden; }
  body::before {
    content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 0;
    background-image: linear-gradient(rgba(232,68,26,0.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(232,68,26,0.03) 1px, transparent 1px);
    background-size: 40px 40px;
  }
  .container { position: relative; z-index: 1; max-width: 900px; margin: 0 auto; padding: 40px 24px 80px; }
  header { display: flex; align-items: flex-end; gap: 16px; margin-bottom: 28px; padding-bottom: 24px; border-bottom: 1px solid var(--border); }
  .logo-mark { width: 48px; height: 48px; background: var(--accent); display: flex; align-items: center; justify-content: center; font-size: 22px; flex-shrink: 0; clip-path: polygon(0 0, 85% 0, 100% 15%, 100% 100%, 15% 100%, 0 85%); }
  h1 { font-size: 28px; font-weight: 800; letter-spacing: -0.5px; line-height: 1; }
  h1 span { color: var(--accent); }
  .version { font-family: var(--mono); font-size: 11px; color: var(--muted); margin-left: auto; padding-bottom: 4px; }

  /* Tabs */
  .tabs { display: flex; gap: 4px; margin-bottom: 24px; border-bottom: 1px solid var(--border); }
  .tab { background: transparent; border: none; color: var(--muted); font-family: var(--sans); font-size: 13px; font-weight: 700; letter-spacing: 1.5px; text-transform: uppercase; padding: 14px 22px; cursor: pointer; border-bottom: 2px solid transparent; transition: color 0.15s, border-color 0.15s; }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-content { display: none; }
  .tab-content.active { display: block; animation: fadeIn 0.2s ease-out; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }

  /* Cards / inputs shared */
  .card { background: var(--surface); border: 1px solid var(--border); padding: 24px; margin-bottom: 20px; }
  .card-title { font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: var(--muted); margin-bottom: 16px; }
  .input-row { display: flex; gap: 10px; }
  input[type="text"] { flex: 1; background: var(--bg); border: 1px solid var(--border); color: var(--text); font-family: var(--mono); font-size: 13px; padding: 12px 16px; outline: none; transition: border-color 0.2s; }
  input[type="text"]:focus { border-color: var(--accent); }
  input[type="text"]::placeholder { color: var(--muted); }
  .btn { background: var(--accent); color: #fff; border: none; font-family: var(--sans); font-size: 13px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; padding: 12px 22px; cursor: pointer; transition: background 0.15s, transform 0.1s; white-space: nowrap; }
  .btn:hover { background: #ff5a30; }
  .btn:active { transform: scale(0.98); }
  .btn:disabled { background: var(--border); color: var(--muted); cursor: not-allowed; transform: none; }
  .btn-ghost { background: transparent; border: 1px solid var(--border); color: var(--text); }
  .btn-ghost:hover { border-color: var(--accent); background: transparent; color: var(--accent); }
  .btn-sm { font-size: 11px; padding: 6px 14px; }
  .btn-success { background: var(--success); }
  .btn-success:hover { background: #2eb87a; }

  /* MDF-specific styles */
  #manga-info { display: none; align-items: center; gap: 16px; padding: 16px 20px; background: var(--surface2); border: 1px solid var(--border); border-left: 3px solid var(--accent); margin-bottom: 20px; }
  .manga-title-display { font-size: 18px; font-weight: 700; }
  .manga-meta { font-family: var(--mono); font-size: 11px; color: var(--muted); }
  .gap-alert { display: none; background: rgba(247,162,62,0.08); border: 1px solid rgba(247,162,62,0.3); border-left: 3px solid var(--warn); padding: 12px 16px; margin-bottom: 16px; font-size: 13px; color: var(--warn); }
  .gap-alert strong { display: block; margin-bottom: 4px; font-size: 12px; letter-spacing: 1px; text-transform: uppercase; }
  #chapter-section { display: none; }
  .chapter-controls { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; }
  .chapter-controls .spacer { flex: 1; }
  .filter-input { background: var(--bg); border: 1px solid var(--border); color: var(--text); font-family: var(--mono); font-size: 12px; padding: 6px 12px; width: 160px; outline: none; }
  .filter-input:focus { border-color: var(--accent); }
  .volume-header { display: flex; align-items: center; gap: 10px; padding: 8px 16px; background: var(--surface2); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 2; user-select: none; cursor: pointer; }
  .volume-header:hover { background: #22222e; }
  .vol-label { font-family: var(--mono); font-size: 10px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: var(--accent2); }
  .vol-cbz-badge { font-family: var(--mono); font-size: 9px; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); border: 1px solid var(--border); padding: 1px 6px; border-radius: 2px; }
  .vol-meta { font-family: var(--mono); font-size: 10px; color: var(--muted); margin-left: auto; }
  .chapter-list { max-height: 420px; overflow-y: auto; border: 1px solid var(--border); }
  .chapter-list::-webkit-scrollbar { width: 4px; }
  .chapter-list::-webkit-scrollbar-track { background: var(--bg); }
  .chapter-list::-webkit-scrollbar-thumb { background: var(--border); }
  .chapter-row { display: flex; align-items: center; gap: 12px; padding: 10px 16px; border-bottom: 1px solid var(--border); cursor: pointer; transition: background 0.1s; user-select: none; }
  .chapter-row:last-child { border-bottom: none; }
  .chapter-row:hover { background: var(--surface2); }
  .chapter-row.selected { background: rgba(232,68,26,0.07); }
  .chapter-row input[type="checkbox"] { accent-color: var(--accent); width: 14px; height: 14px; flex-shrink: 0; }
  .ch-num { font-family: var(--mono); font-size: 12px; font-weight: 500; color: var(--accent); width: 60px; flex-shrink: 0; }
  .ch-title { font-size: 13px; flex: 1; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ch-pages { font-family: var(--mono); font-size: 11px; color: var(--muted); flex-shrink: 0; }
  .ch-status { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--border); }
  .ch-status.done { background: var(--success); }
  .ch-status.error { background: var(--accent); }
  .ch-status.downloading { background: var(--warn); animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  #volume-summary { display: none; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  .vol-pill { background: var(--surface2); border: 1px solid var(--border); border-radius: 2px; padding: 6px 12px; font-family: var(--mono); font-size: 11px; color: var(--muted); }
  .vol-pill strong { color: var(--accent2); }
  .cbz-toggle-row { display: flex; align-items: center; gap: 12px; padding: 14px 0 16px; border-top: 1px solid var(--border); margin-top: 4px; }
  .toggle-wrap { position: relative; width: 40px; height: 22px; flex-shrink: 0; }
  .toggle-wrap input { opacity: 0; width: 0; height: 0; }
  .toggle-slider { position: absolute; inset: 0; background: var(--border); border-radius: 22px; cursor: pointer; transition: background 0.2s; }
  .toggle-slider::before { content: ''; position: absolute; width: 16px; height: 16px; left: 3px; top: 3px; background: var(--muted); border-radius: 50%; transition: transform 0.2s, background 0.2s; }
  .toggle-wrap input:checked + .toggle-slider { background: rgba(232,68,26,0.3); }
  .toggle-wrap input:checked + .toggle-slider::before { transform: translateX(18px); background: var(--accent); }
  .cbz-label { font-size: 13px; font-weight: 600; }
  .cbz-sublabel { font-family: var(--mono); font-size: 11px; color: var(--muted); }
  .cbz-progress-section { display: none; margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border); }
  .cbz-vol-list { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
  .cbz-vol-row { display: flex; align-items: center; gap: 10px; font-family: var(--mono); font-size: 11px; color: var(--muted); }
  .cbz-vol-icon { width: 14px; text-align: center; }
  .cbz-vol-icon.done { color: var(--success); }
  .cbz-vol-icon.building { color: var(--warn); animation: pulse 1s infinite; }
  .cbz-vol-icon.err { color: var(--accent); }
  .outdir-row { display: flex; gap: 10px; align-items: center; margin-bottom: 16px; }
  .outdir-label { font-family: var(--mono); font-size: 11px; color: var(--muted); white-space: nowrap; }
  #progress-section { display: none; }
  .overall-progress { margin-bottom: 20px; }
  .progress-label { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 11px; color: var(--muted); margin-bottom: 6px; }
  .progress-bar-wrap { background: var(--bg); border: 1px solid var(--border); height: 6px; }
  .progress-bar-fill { height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent2)); transition: width 0.3s ease; width: 0%; }
  .current-chapter-info { font-family: var(--mono); font-size: 12px; color: var(--accent2); margin-bottom: 12px; }
  .log-box { background: var(--bg); border: 1px solid var(--border); font-family: var(--mono); font-size: 11px; color: var(--muted); padding: 12px 16px; max-height: 160px; overflow-y: auto; line-height: 1.8; }
  .log-box::-webkit-scrollbar { width: 3px; }
  .log-box::-webkit-scrollbar-thumb { background: var(--border); }
  .log-line { display: block; }
  .log-line.ok { color: var(--success); }
  .log-line.err { color: var(--accent); }
  .log-line.info { color: var(--accent2); }
  .log-line.skip { color: var(--muted); }
  .log-line.warn { color: var(--warn); }
  .done-banner { display: none; background: rgba(62,207,142,0.08); border: 1px solid rgba(62,207,142,0.3); border-left: 3px solid var(--success); padding: 16px 20px; margin-top: 16px; font-weight: 700; color: var(--success); font-size: 14px; letter-spacing: 0.5px; }
  .done-actions { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
  #loading-spinner, #cbz-loading-spinner { display: none; font-family: var(--mono); font-size: 12px; color: var(--muted); margin-top: 10px; }
  .spinner { display: inline-block; animation: spin 1s linear infinite; margin-right: 6px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .selection-count { font-family: var(--mono); font-size: 11px; color: var(--muted); }

  /* CBZ Processor tab */
  #cbz-file-list { display: flex; flex-direction: column; gap: 8px; }
  .cbz-file-row { display: flex; align-items: center; gap: 12px; padding: 12px 16px; background: var(--surface2); border: 1px solid var(--border); transition: border-color 0.2s; }
  .cbz-file-row.status-active { border-color: var(--accent); }
  .cbz-file-row.status-done   { border-color: rgba(62,207,142,0.45); }
  .cbz-file-row.status-error  { border-color: rgba(224,82,82,0.5); }
  .cbz-file-icon { width: 32px; height: 32px; background: rgba(232,68,26,0.12); border: 1px solid rgba(232,68,26,0.4); display: flex; align-items: center; justify-content: center; font-size: 14px; flex-shrink: 0; }
  .cbz-file-details { flex: 1; min-width: 0; }
  .cbz-file-name { font-family: var(--mono); font-size: 12px; font-weight: 500; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .cbz-file-meta { font-family: var(--mono); font-size: 10px; color: var(--muted); margin-top: 2px; }
  .cbz-chapter-wrap { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  .cbz-badge { font-family: var(--mono); font-size: 9px; padding: 2px 7px; font-weight: 700; letter-spacing: 0.5px; border: 1px solid; }
  .cbz-badge.auto   { background: rgba(62,207,142,0.12); color: var(--success); border-color: rgba(62,207,142,0.35); }
  .cbz-badge.manual { background: rgba(247,162,62,0.12); color: var(--warn);    border-color: rgba(247,162,62,0.35); }
  .cbz-chapter-input-group { display: flex; align-items: stretch; border: 1px solid var(--border); background: var(--bg); }
  .cbz-chapter-input-group:focus-within { border-color: var(--accent); }
  .cbz-chapter-prefix { font-family: var(--mono); font-size: 11px; color: var(--muted); padding: 6px 8px; border-right: 1px solid var(--border); background: var(--surface); display: flex; align-items: center; }
  .cbz-chapter-input { border: none; background: transparent; color: var(--text); font-family: var(--mono); font-size: 12px; font-weight: 600; padding: 6px 8px; width: 80px; outline: none; }
  .cbz-file-remove { background: transparent; border: none; color: var(--muted); cursor: pointer; font-size: 14px; padding: 4px 8px; flex-shrink: 0; transition: color 0.15s; }
  .cbz-file-remove:hover { color: var(--danger); }
  .cbz-status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--border); }
  .cbz-status-dot.active { background: var(--warn); animation: pulse 1s infinite; }
  .cbz-status-dot.done { background: var(--success); }
  .cbz-status-dot.error { background: var(--danger); }
  .cbz-settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 640px) { .cbz-settings-grid { grid-template-columns: 1fr; } }
  .cbz-field-label { font-family: var(--mono); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; display: block; }
  .cbz-field-hint { font-family: var(--mono); font-size: 10px; color: var(--muted); margin-top: 4px; }
  .cbz-mode-row { display: flex; gap: 8px; margin-top: 10px; }
  .cbz-mode-btn { flex: 1; background: var(--bg); border: 1px solid var(--border); color: var(--muted); font-family: var(--mono); font-size: 11px; letter-spacing: 1px; text-transform: uppercase; padding: 10px; cursor: pointer; transition: all 0.15s; }
  .cbz-mode-btn:hover { color: var(--text); border-color: var(--accent2); }
  .cbz-mode-btn.active { background: rgba(232,68,26,0.08); border-color: var(--accent); color: var(--accent); }
  .empty-state { padding: 32px; text-align: center; color: var(--muted); font-family: var(--mono); font-size: 12px; border: 1px dashed var(--border); }
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="logo-mark">⚙️</div>
    <div>
      <h1>Manga<span>Factory</span></h1>
      <div style="font-size:12px; color:var(--muted); margin-top:3px;">Download · Process · Package</div>
    </div>
    <div class="version">v1.0.0</div>
  </header>

  <div class="tabs">
    <button class="tab active" data-tab="download">📥 Download</button>
    <button class="tab" data-tab="cbz">📦 CBZ Processor</button>
  </div>

  <!-- ─── DOWNLOAD TAB ──────────────────────────────────────────────────────── -->
  <div class="tab-content active" id="tab-download">

    <div class="card">
      <div class="card-title">Series URL or ID</div>
      <div class="input-row">
        <input type="text" id="url-input" placeholder="https://mangadex.org/title/... or UUID" />
        <button class="btn" id="fetch-btn" onclick="fetchSeries()">Fetch</button>
      </div>
      <div id="loading-spinner"><span class="spinner">◌</span> Fetching chapter list...</div>
    </div>

    <div id="manga-info">
      <div>
        <div class="manga-title-display" id="manga-title-display">—</div>
        <div class="manga-meta" id="manga-meta">—</div>
      </div>
    </div>

    <div class="gap-alert" id="gap-alert">
      <strong>⚠ Missing Chapters Detected</strong>
      <span id="gap-text"></span>
    </div>

    <div id="volume-summary"></div>

    <div id="chapter-section">
      <div class="card">
        <div class="card-title">Chapters</div>
        <div class="chapter-controls">
          <button class="btn btn-ghost btn-sm" onclick="selectAll()">Select All</button>
          <button class="btn btn-ghost btn-sm" onclick="selectNone()">Clear</button>
          <span class="selection-count" id="selection-count">0 selected</span>
          <span class="spacer"></span>
          <input class="filter-input" id="filter-input" placeholder="Filter chapters..." oninput="filterChapters()" />
        </div>
        <div class="chapter-list" id="chapter-list"></div>
      </div>

      <div class="card">
        <div class="card-title">Download Settings</div>
        <div class="outdir-row">
          <div class="outdir-label">Output Folder:</div>
          <input type="text" id="output-dir" style="flex:1; font-size:12px;" value="~/Downloads/manga" />
        </div>
        <div class="cbz-toggle-row">
          <label class="toggle-wrap">
            <input type="checkbox" id="cbz-toggle">
            <span class="toggle-slider"></span>
          </label>
          <div>
            <div class="cbz-label">Package into CBZ volumes</div>
            <div class="cbz-sublabel">Groups chapters by MangaDex volume → creates one .cbz per volume</div>
          </div>
        </div>
        <button class="btn btn-success" id="dl-btn" onclick="startDownload()">Download Selected</button>
      </div>
    </div>

    <div id="progress-section">
      <div class="card">
        <div class="card-title">Progress</div>
        <div class="current-chapter-info" id="current-chapter-info">Starting...</div>
        <div class="overall-progress">
          <div class="progress-label"><span>Pages</span><span id="page-progress-text">0 / 0</span></div>
          <div class="progress-bar-wrap"><div class="progress-bar-fill" id="page-bar"></div></div>
        </div>
        <div class="overall-progress">
          <div class="progress-label"><span>Chapters</span><span id="ch-progress-text">0 / 0</span></div>
          <div class="progress-bar-wrap">
            <div class="progress-bar-fill" id="ch-bar" style="background: linear-gradient(90deg, var(--success), #2eb87a);"></div>
          </div>
        </div>
        <div class="log-box" id="log-box"></div>
        <div class="cbz-progress-section" id="cbz-progress-section">
          <div class="card-title" style="margin-bottom:10px;">📦 Building CBZ Volumes</div>
          <div class="cbz-vol-list" id="cbz-vol-list"></div>
        </div>
        <div class="done-banner" id="done-banner">✓ Done!</div>
        <div class="done-actions">
          <button class="btn btn-ghost btn-sm" id="cancel-btn" onclick="cancelDownload()">Cancel</button>
          <button class="btn btn-sm" id="send-to-cbz-btn" style="display:none" onclick="sendToCbzProcessor()">📦 Send to CBZ Processor →</button>
        </div>
      </div>
    </div>

  </div>

  <!-- ─── CBZ PROCESSOR TAB ────────────────────────────────────────────────── -->
  <div class="tab-content" id="tab-cbz">

    <div class="card">
      <div class="card-title">Source Folder</div>
      <div class="input-row">
        <input type="text" id="cbz-source-input" placeholder="~/Downloads/manga/series_slug" />
        <button class="btn" id="cbz-scan-btn" onclick="cbzScan()">Scan</button>
      </div>
      <div id="cbz-loading-spinner"><span class="spinner">◌</span> Scanning...</div>
      <div class="cbz-field-hint">Folder containing one or more .cbz files. Chapter numbers will be auto-detected from filenames.</div>
    </div>

    <div id="cbz-file-section" style="display:none">
      <div class="card">
        <div class="card-title">Files Detected</div>
        <div class="chapter-controls">
          <button class="btn btn-ghost btn-sm" id="cbz-autofill-btn" onclick="cbzAutofill()">↻ Auto-fill</button>
          <button class="btn btn-ghost btn-sm" onclick="cbzClearFiles()">Clear</button>
          <span class="selection-count" id="cbz-file-count">0 files</span>
        </div>
        <div id="cbz-file-list"></div>
      </div>

      <div class="card">
        <div class="card-title">Volume & Cover</div>
        <div class="cbz-settings-grid">
          <div>
            <label class="cbz-field-label">Volume Number</label>
            <div class="input-row">
              <input type="text" id="cbz-volume-input" placeholder="e.g. 03" />
            </div>
            <div class="cbz-field-hint">Folder/CBZ named: <span id="cbz-volume-preview" style="color:var(--accent2)">New Volume</span></div>
          </div>
          <div>
            <label class="cbz-field-label">Cover Image Path (optional)</label>
            <div class="input-row">
              <input type="text" id="cbz-cover-input" placeholder="~/Pictures/cover.jpg" />
            </div>
            <div class="cbz-field-hint">Saved as <code style="color:var(--accent)">000_cover.{ext}</code> — always first.</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">Output</div>
        <div class="outdir-row">
          <div class="outdir-label">Output Folder:</div>
          <input type="text" id="cbz-output-dir" style="flex:1; font-size:12px;" value="~/Downloads/manga/output" />
        </div>
        <label class="cbz-field-label" style="margin-top: 10px;">Output Mode</label>
        <div class="cbz-mode-row">
          <button class="cbz-mode-btn active" id="cbz-mode-cbz" onclick="cbzSetMode('cbz')">📦 Single CBZ</button>
          <button class="cbz-mode-btn" id="cbz-mode-folder" onclick="cbzSetMode('folder')">📁 Folder Tree</button>
        </div>
        <div class="cbz-field-hint" id="cbz-mode-hint" style="margin-top: 8px;">Packages everything into a single Volume_XX.cbz file.</div>
        <button class="btn btn-success" id="cbz-process-btn" onclick="cbzStart()" style="margin-top: 18px;">Process Files</button>
      </div>
    </div>

    <div id="cbz-empty-state" class="empty-state">Enter a folder path above and click Scan to begin.</div>

    <div id="cbz-progress-wrap" style="display:none">
      <div class="card">
        <div class="card-title">Progress</div>
        <div class="current-chapter-info" id="cbz-current-info">Starting...</div>
        <div class="overall-progress">
          <div class="progress-label"><span>Pages</span><span id="cbz-page-progress-text">0 / 0</span></div>
          <div class="progress-bar-wrap"><div class="progress-bar-fill" id="cbz-page-bar"></div></div>
        </div>
        <div class="overall-progress">
          <div class="progress-label"><span>Files</span><span id="cbz-file-progress-text">0 / 0</span></div>
          <div class="progress-bar-wrap">
            <div class="progress-bar-fill" id="cbz-file-bar" style="background: linear-gradient(90deg, var(--success), #2eb87a);"></div>
          </div>
        </div>
        <div class="log-box" id="cbz-log-box"></div>
        <div class="done-banner" id="cbz-done-banner">✓ Done!</div>
        <div class="done-actions">
          <button class="btn btn-ghost btn-sm" id="cbz-cancel-btn" onclick="cbzCancel()">Cancel</button>
        </div>
      </div>
    </div>

  </div>
</div>

<script>
/* ─────────────────────────────────────────────────────────────────────────
   Tab switching
   ───────────────────────────────────────────────────────────────────────── */
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
  });
});

/* ─────────────────────────────────────────────────────────────────────────
   MangaDex download tab (unchanged behaviour from MangaDexFactory 2.0)
   ───────────────────────────────────────────────────────────────────────── */
let allChapters = [], allVolumes = [], mangaInfo = null, sessionId = null, eventSource = null;
let doneChs = 0, totalChs = 0;
let lastDownloadContext = null;   // used to pre-fill the CBZ processor

async function fetchSeries() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) return;
  document.getElementById('fetch-btn').disabled = true;
  document.getElementById('loading-spinner').style.display = 'block';
  document.getElementById('chapter-section').style.display = 'none';
  document.getElementById('manga-info').style.display = 'none';
  document.getElementById('gap-alert').style.display = 'none';
  document.getElementById('volume-summary').style.display = 'none';
  try {
    const res = await fetch('/api/fetch', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url}) });
    const data = await res.json();
    if (data.error) { alert('Error: ' + data.error); return; }
    mangaInfo = data.manga;
    allChapters = data.chapters;
    allVolumes = data.volumes || [];
    document.getElementById('manga-title-display').textContent = data.manga.title;
    document.getElementById('manga-meta').textContent = `${allChapters.length} chapters · ${allVolumes.length} volumes · ${allChapters.reduce((s,c)=>s+c.pages,0)} total pages`;
    document.getElementById('manga-info').style.display = 'flex';
    if (data.gaps && data.gaps.length > 0) {
      document.getElementById('gap-text').textContent = `Gaps found between: ${data.gaps.map(g=>`Ch.${g.from} → Ch.${g.to}`).join(', ')}. These chapters may not be translated yet.`;
      document.getElementById('gap-alert').style.display = 'block';
    }
    renderVolumeSummary(allVolumes);
    renderChapterList(allChapters);
    document.getElementById('chapter-section').style.display = 'block';
  } catch(e) { alert('Failed: ' + e.message); }
  finally { document.getElementById('fetch-btn').disabled = false; document.getElementById('loading-spinner').style.display = 'none'; }
}

function renderVolumeSummary(volumes) {
  const wrap = document.getElementById('volume-summary');
  wrap.innerHTML = '';
  volumes.forEach(v => {
    const pill = document.createElement('div');
    pill.className = 'vol-pill';
    pill.innerHTML = `<strong>${v.label}</strong> &nbsp;${v.chapter_count}ch · ${v.page_count}p`;
    wrap.appendChild(pill);
  });
  wrap.style.display = volumes.length ? 'flex' : 'none';
}

function renderChapterList(chapters) {
  const list = document.getElementById('chapter-list');
  list.innerHTML = '';
  const groups = {}, groupOrder = [];
  chapters.forEach(ch => {
    const vol = (ch.volume || '').trim() || 'unnumbered';
    if (!groups[vol]) { groups[vol] = []; groupOrder.push(vol); }
    groups[vol].push(ch);
  });
  groupOrder.forEach(volKey => {
    const chs = groups[volKey];
    const volLabel = volKey === 'unnumbered' ? 'Unnumbered Chapters' : `Volume ${volKey}`;
    const totalPages = chs.reduce((s,c)=>s+c.pages,0);
    const header = document.createElement('div');
    header.className = 'volume-header';
    header.dataset.vol = volKey;
    header.innerHTML = `<div class="vol-label">▸ ${volLabel}</div><div class="vol-cbz-badge">cbz</div><div class="vol-meta">${chs.length} ch · ${totalPages}p</div>`;
    header.onclick = () => toggleVolumeSelect(volKey);
    list.appendChild(header);
    chs.forEach(ch => {
      const row = document.createElement('div');
      row.className = 'chapter-row';
      row.dataset.id = ch.id;
      row.dataset.vol = volKey;
      row.innerHTML = `<input type="checkbox" class="ch-checkbox" data-id="${ch.id}" data-vol="${volKey}" onchange="updateCount()"><div class="ch-num">Ch.${ch.chapter || '?'}</div><div class="ch-title">${ch.title || '—'}</div><div class="ch-pages">${ch.pages}p</div><div class="ch-status" id="status-${ch.id}"></div>`;
      row.onclick = (e) => { if (e.target.tagName === 'INPUT') return; const cb = row.querySelector('input[type=checkbox]'); cb.checked = !cb.checked; row.classList.toggle('selected', cb.checked); updateCount(); };
      list.appendChild(row);
    });
  });
  updateCount();
}

function toggleVolumeSelect(volKey) {
  const cbs = document.querySelectorAll(`.ch-checkbox[data-vol="${volKey}"]`);
  const anyUnchecked = [...cbs].some(cb => !cb.checked);
  cbs.forEach(cb => { cb.checked = anyUnchecked; cb.closest('.chapter-row').classList.toggle('selected', anyUnchecked); });
  updateCount();
}

function filterChapters() {
  const q = document.getElementById('filter-input').value.toLowerCase();
  document.querySelectorAll('.chapter-row').forEach(row => { row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none'; });
  document.querySelectorAll('.volume-header').forEach(hdr => {
    const anyVisible = [...document.querySelectorAll(`.chapter-row[data-vol="${hdr.dataset.vol}"]`)].some(r => r.style.display !== 'none');
    hdr.style.display = anyVisible ? '' : 'none';
  });
}

function selectAll() { document.querySelectorAll('.ch-checkbox').forEach(cb => { cb.checked = true; cb.closest('.chapter-row').classList.add('selected'); }); updateCount(); }
function selectNone() { document.querySelectorAll('.ch-checkbox').forEach(cb => { cb.checked = false; cb.closest('.chapter-row').classList.remove('selected'); }); updateCount(); }
function updateCount() { document.getElementById('selection-count').textContent = `${document.querySelectorAll('.ch-checkbox:checked').length} selected`; }
function getSelectedChapters() { const ids = [...document.querySelectorAll('.ch-checkbox:checked')].map(cb => cb.dataset.id); return allChapters.filter(ch => ids.includes(ch.id)); }

async function startDownload() {
  const selected = getSelectedChapters();
  if (!selected.length) { alert('Select at least one chapter.'); return; }
  const outputDir = document.getElementById('output-dir').value.trim();
  const makeCbz = document.getElementById('cbz-toggle').checked;
  document.getElementById('dl-btn').disabled = true;
  document.getElementById('progress-section').style.display = 'block';
  document.getElementById('done-banner').style.display = 'none';
  document.getElementById('send-to-cbz-btn').style.display = 'none';
  document.getElementById('log-box').innerHTML = '';
  document.getElementById('cbz-progress-section').style.display = 'none';
  document.getElementById('cbz-vol-list').innerHTML = '';
  totalChs = selected.length; doneChs = 0;
  updateChProgress(0, totalChs); updatePageProgress(0, 0);
  const res = await fetch('/api/download', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ manga_id: mangaInfo.id, manga_title: mangaInfo.title, chapter_ids: selected, output_dir: outputDir, make_cbz: makeCbz }) });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }
  sessionId = data.session_id;
  lastDownloadContext = { output_dir: data.output_dir, title: mangaInfo.title };
  log(`Output: ${data.output_dir}`, 'info');
  if (makeCbz) log('CBZ packaging enabled — volumes will be built after download.', 'info');
  eventSource = new EventSource(`/api/stream/${sessionId}`);
  let curChTotal = 0, curChDone = 0;
  eventSource.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'ping') return;
    if (msg.type === 'chapter_start') { curChTotal = msg.total; curChDone = 0; document.getElementById('current-chapter-info').textContent = `Downloading Chapter ${msg.chapter} (${msg.total} pages)`; log(`─── Chapter ${msg.chapter} ───`, 'info'); updatePageProgress(0, msg.total); }
    if (msg.type === 'page_done') { curChDone++; log(msg.skipped ? `  skip  ${msg.file}` : `  ✓  ${msg.file}`, msg.skipped ? 'skip' : 'ok'); updatePageProgress(curChDone, curChTotal); }
    if (msg.type === 'page_error') { log(`  ✗  page ${msg.page}: ${msg.error}`, 'err'); }
    if (msg.type === 'chapter_done') { doneChs++; updateChProgress(doneChs, totalChs); const s = document.getElementById(`status-${getChIdByNum(msg.chapter)}`); if (s) s.className = 'ch-status done'; }
    if (msg.type === 'chapter_error') { log(`  ✗  Chapter ${msg.chapter} failed: ${msg.error}`, 'err'); doneChs++; updateChProgress(doneChs, totalChs); }
    if (msg.type === 'cbz_start') { document.getElementById('cbz-progress-section').style.display = 'block'; document.getElementById('current-chapter-info').textContent = `Building ${msg.total} CBZ volume${msg.total > 1 ? 's' : ''}...`; log(`─── Packaging ${msg.total} CBZ volume(s) ───`, 'info'); }
    if (msg.type === 'cbz_building') { const vl = msg.vol === 'unnumbered' ? 'Unnumbered' : `Vol. ${msg.vol}`; addCbzRow(msg.vol, `building-${msg.vol}`, '⧗', 'building', `${vl} → ${msg.cbz} (${msg.file_count} files)`); }
    if (msg.type === 'cbz_done') { const vl = msg.vol === 'unnumbered' ? 'Unnumbered' : `Vol. ${msg.vol}`; updateCbzRow(`building-${msg.vol}`, '✓', 'done', `${vl} → ${msg.cbz}`); log(`  ✓  ${msg.cbz}`, 'ok'); }
    if (msg.type === 'cbz_error') { updateCbzRow(`building-${msg.vol}`, '✗', 'err', `Vol. ${msg.vol} failed: ${msg.error}`); log(`  ✗  CBZ Vol. ${msg.vol}: ${msg.error}`, 'err'); }
    if (msg.type === 'all_done') {
      eventSource.close();
      const cbzOn = document.getElementById('cbz-toggle').checked;
      document.getElementById('done-banner').style.display = 'block';
      document.getElementById('done-banner').textContent = cbzOn ? '✓ All chapters downloaded and CBZ volumes packaged.' : '✓ All chapters downloaded successfully.';
      document.getElementById('current-chapter-info').textContent = 'Complete!';
      document.getElementById('cancel-btn').textContent = 'Done';
      document.getElementById('send-to-cbz-btn').style.display = 'inline-block';
    }
  };
}

function addCbzRow(key, id, icon, iconClass, text) { const list = document.getElementById('cbz-vol-list'); const row = document.createElement('div'); row.className = 'cbz-vol-row'; row.id = `cbz-row-${id}`; row.innerHTML = `<div class="cbz-vol-icon ${iconClass}" id="cbz-icon-${id}">${icon}</div><div>${text}</div>`; list.appendChild(row); }
function updateCbzRow(id, icon, iconClass, text) { const iconEl = document.getElementById(`cbz-icon-${id}`); const row = document.getElementById(`cbz-row-${id}`); if (iconEl) { iconEl.textContent = icon; iconEl.className = `cbz-vol-icon ${iconClass}`; } if (row) row.querySelector('div:last-child').textContent = text; }
function getChIdByNum(num) { const ch = allChapters.find(c => c.chapter == num); return ch ? ch.id : ''; }
function updatePageProgress(done, total) { const pct = total > 0 ? (done / total * 100) : 0; document.getElementById('page-bar').style.width = pct + '%'; document.getElementById('page-progress-text').textContent = `${done} / ${total}`; }
function updateChProgress(done, total) { const pct = total > 0 ? (done / total * 100) : 0; document.getElementById('ch-bar').style.width = pct + '%'; document.getElementById('ch-progress-text').textContent = `${done} / ${total}`; }
function log(msg, cls = '') { const box = document.getElementById('log-box'); const line = document.createElement('span'); line.className = 'log-line ' + cls; line.textContent = msg; box.appendChild(line); box.appendChild(document.createElement('br')); box.scrollTop = box.scrollHeight; }
async function cancelDownload() { if (eventSource) eventSource.close(); if (sessionId) { await fetch(`/api/cancel/${sessionId}`, {method: 'POST'}); sessionId = null; } document.getElementById('dl-btn').disabled = false; document.getElementById('cancel-btn').textContent = 'Cancel'; }
document.getElementById('url-input').addEventListener('keydown', e => { if (e.key === 'Enter') fetchSeries(); });

/* Send the just-downloaded output folder over to the CBZ processor tab. */
function sendToCbzProcessor() {
  if (!lastDownloadContext) return;
  // Pre-fill source folder and switch tabs
  document.getElementById('cbz-source-input').value = lastDownloadContext.output_dir;
  const outBase = lastDownloadContext.output_dir.replace(/[\\/]+$/, '');
  document.getElementById('cbz-output-dir').value = outBase + '/processed';
  // Switch to CBZ tab
  document.querySelector('.tab[data-tab="cbz"]').click();
  // Auto-scan
  cbzScan();
}

/* ─────────────────────────────────────────────────────────────────────────
   CBZ Processor tab
   ───────────────────────────────────────────────────────────────────────── */
let cbzQueue = [];        // [{path, name, size, detected_chapter, chapter}]
let cbzMode = 'cbz';      // 'cbz' | 'folder'
let cbzSessionId = null;
let cbzEventSource = null;
let cbzNextId = 0;

function cbzFormatSize(bytes) {
  if (bytes < 1024)    return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

async function cbzScan() {
  const folder = document.getElementById('cbz-source-input').value.trim();
  if (!folder) { alert('Enter a folder path.'); return; }
  document.getElementById('cbz-scan-btn').disabled = true;
  document.getElementById('cbz-loading-spinner').style.display = 'block';
  try {
    const res = await fetch('/api/cbz/scan', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({folder}) });
    const data = await res.json();
    if (data.error) { alert('Error: ' + data.error); return; }
    cbzQueue = data.files.map(f => ({ id: cbzNextId++, ...f, chapter: f.detected_chapter }));
    cbzRenderFiles();
    document.getElementById('cbz-file-section').style.display = cbzQueue.length ? 'block' : 'none';
    document.getElementById('cbz-empty-state').style.display = cbzQueue.length ? 'none' : 'block';
    if (!cbzQueue.length) document.getElementById('cbz-empty-state').textContent = 'No .cbz files found in that folder.';
  } catch (e) {
    alert('Failed: ' + e.message);
  } finally {
    document.getElementById('cbz-scan-btn').disabled = false;
    document.getElementById('cbz-loading-spinner').style.display = 'none';
  }
}

function cbzRenderFiles() {
  const list = document.getElementById('cbz-file-list');
  list.innerHTML = '';
  cbzQueue.forEach(item => list.appendChild(cbzRenderRow(item)));
  document.getElementById('cbz-file-count').textContent = `${cbzQueue.length} file${cbzQueue.length !== 1 ? 's' : ''}`;
}

function cbzRenderRow(item) {
  const el = document.createElement('div');
  el.className = 'cbz-file-row';
  el.id = `cbz-row-${item.id}`;
  const isAuto = item.chapter !== '' && item.chapter === item.detected_chapter;
  el.innerHTML = `
    <div class="cbz-status-dot" id="cbz-dot-${item.id}"></div>
    <div class="cbz-file-icon">📚</div>
    <div class="cbz-file-details">
      <div class="cbz-file-name" title="${item.name}">${item.name}</div>
      <div class="cbz-file-meta">${cbzFormatSize(item.size)}</div>
    </div>
    <div class="cbz-chapter-wrap">
      <span class="cbz-badge ${isAuto ? 'auto' : 'manual'}" id="cbz-badge-${item.id}">${isAuto ? 'AUTO' : 'MANUAL'}</span>
      <div class="cbz-chapter-input-group">
        <span class="cbz-chapter-prefix">Chapter_</span>
        <input type="text" class="cbz-chapter-input" id="cbz-chinput-${item.id}" placeholder="e.g. 042" value="${item.chapter}">
      </div>
    </div>
    <button class="cbz-file-remove" title="Remove">✕</button>
  `;
  el.querySelector(`#cbz-chinput-${item.id}`).addEventListener('input', e => {
    item.chapter = e.target.value.trim();
    const auto = item.chapter !== '' && item.chapter === item.detected_chapter;
    const b = document.getElementById(`cbz-badge-${item.id}`);
    b.textContent = auto ? 'AUTO' : 'MANUAL';
    b.className = `cbz-badge ${auto ? 'auto' : 'manual'}`;
  });
  el.querySelector('.cbz-file-remove').addEventListener('click', () => {
    cbzQueue = cbzQueue.filter(q => q.id !== item.id);
    el.remove();
    document.getElementById('cbz-file-count').textContent = `${cbzQueue.length} file${cbzQueue.length !== 1 ? 's' : ''}`;
  });
  return el;
}

function cbzAutofill() {
  const firstFilled = cbzQueue.find(q => q.chapter.trim() !== '');
  if (!firstFilled) return;
  const firstVal = firstFilled.chapter.trim();
  const padLen = firstVal.length;
  let num = parseInt(firstVal, 10);
  if (isNaN(num)) return;
  let filling = false;
  cbzQueue.forEach(item => {
    if (item.id === firstFilled.id) { filling = true; return; }
    if (!filling) return;
    num++;
    const newVal = String(num).padStart(padLen, '0');
    item.chapter = newVal;
    item.detected_chapter = '';
    const input = document.getElementById(`cbz-chinput-${item.id}`);
    const badge = document.getElementById(`cbz-badge-${item.id}`);
    if (input) input.value = newVal;
    if (badge) { badge.textContent = 'AUTO-FILL'; badge.className = 'cbz-badge manual'; }
  });
}

function cbzClearFiles() {
  cbzQueue = [];
  cbzRenderFiles();
  document.getElementById('cbz-file-section').style.display = 'none';
  document.getElementById('cbz-empty-state').style.display = 'block';
  document.getElementById('cbz-empty-state').textContent = 'Enter a folder path above and click Scan to begin.';
}

document.getElementById('cbz-volume-input').addEventListener('input', e => {
  const v = e.target.value.trim();
  document.getElementById('cbz-volume-preview').textContent = v ? `Volume_${v}` : 'New Volume';
});

function cbzSetMode(mode) {
  cbzMode = mode;
  document.getElementById('cbz-mode-cbz').classList.toggle('active', mode === 'cbz');
  document.getElementById('cbz-mode-folder').classList.toggle('active', mode === 'folder');
  document.getElementById('cbz-mode-hint').textContent =
    mode === 'cbz'
      ? 'Packages everything into a single Volume_XX.cbz file.'
      : 'Extracts pages into a Volume_XX/ folder tree (no zipping).';
}

async function cbzStart() {
  if (!cbzQueue.length) { alert('No files to process.'); return; }
  for (const item of cbzQueue) {
    if (!item.chapter.trim()) { alert(`Missing chapter number for:\n${item.name}`); return; }
  }
  const volume   = document.getElementById('cbz-volume-input').value.trim();
  const cover    = document.getElementById('cbz-cover-input').value.trim();
  const outDir   = document.getElementById('cbz-output-dir').value.trim();
  if (!outDir) { alert('Specify an output folder.'); return; }

  document.getElementById('cbz-process-btn').disabled = true;
  document.getElementById('cbz-progress-wrap').style.display = 'block';
  document.getElementById('cbz-done-banner').style.display = 'none';
  document.getElementById('cbz-log-box').innerHTML = '';
  cbzUpdateFileProgress(0, cbzQueue.length);
  cbzUpdatePageProgress(0, 0);
  document.getElementById('cbz-cancel-btn').textContent = 'Cancel';

  // Reset row status
  cbzQueue.forEach(it => { const d = document.getElementById(`cbz-dot-${it.id}`); if (d) d.className = 'cbz-status-dot'; });

  const payload = {
    items: cbzQueue.map(it => ({ path: it.path, chapter: it.chapter })),
    volume, cover_path: cover, output_dir: outDir, mode: cbzMode,
  };
  const res = await fetch('/api/cbz/process', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
  const data = await res.json();
  if (data.error) { alert(data.error); document.getElementById('cbz-process-btn').disabled = false; return; }
  cbzSessionId = data.session_id;
  cbzLog(`Output: ${data.output_dir}  (${cbzMode === 'cbz' ? 'single CBZ' : 'folder tree'})`, 'info');

  cbzEventSource = new EventSource(`/api/cbz/stream/${cbzSessionId}`);
  let filesDone = 0, filesTotal = cbzQueue.length;
  let activeItemId = null;

  cbzEventSource.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'ping') return;
    if (msg.type === 'process_start') {
      document.getElementById('cbz-current-info').textContent = `Processing ${msg.total} file${msg.total !== 1 ? 's' : ''} → ${msg.volume}`;
      cbzLog(`─── Processing ${msg.total} file(s) → ${msg.volume} (${msg.mode}) ───`, 'info');
    }
    if (msg.type === 'pages_total') { cbzUpdatePageProgress(0, msg.total); }
    if (msg.type === 'file_start') {
      const item = cbzQueue.find(q => q.name === msg.file);
      if (item) { activeItemId = item.id; const d = document.getElementById(`cbz-dot-${item.id}`); if (d) d.className = 'cbz-status-dot active'; }
      document.getElementById('cbz-current-info').textContent = `File ${msg.index}/${msg.total} — ${msg.file} → Chapter_${msg.chapter} (${msg.page_count} pages)`;
      cbzLog(`─── [${msg.index}/${msg.total}] ${msg.file} → Chapter_${msg.chapter} ───`, 'info');
    }
    if (msg.type === 'page_done') {
      cbzLog(`  ✓  ${msg.file}`, 'ok');
      cbzUpdatePageProgress(msg.pages_done, msg.pages_total);
    }
    if (msg.type === 'file_done') {
      filesDone++;
      cbzUpdateFileProgress(filesDone, filesTotal);
      const item = cbzQueue.find(q => q.name === msg.file);
      if (item) { const d = document.getElementById(`cbz-dot-${item.id}`); if (d) d.className = 'cbz-status-dot done'; }
    }
    if (msg.type === 'file_error') {
      cbzLog(`  ✗  ${msg.file}: ${msg.error}`, 'err');
      const item = cbzQueue.find(q => q.name === msg.file);
      if (item) { const d = document.getElementById(`cbz-dot-${item.id}`); if (d) d.className = 'cbz-status-dot error'; }
    }
    if (msg.type === 'log') cbzLog(msg.text, msg.level || '');
    if (msg.type === 'fatal') cbzLog(`  FATAL: ${msg.error}`, 'err');
    if (msg.type === 'all_done') {
      cbzEventSource.close();
      document.getElementById('cbz-done-banner').style.display = 'block';
      document.getElementById('cbz-done-banner').textContent = msg.mode === 'cbz'
        ? `✓ Done — CBZ saved to ${msg.output_path}`
        : `✓ Done — Folder tree at ${msg.output_path}`;
      document.getElementById('cbz-current-info').textContent = 'Complete!';
      document.getElementById('cbz-cancel-btn').textContent = 'Done';
      document.getElementById('cbz-process-btn').disabled = false;
    }
  };
}

function cbzUpdateFileProgress(done, total) {
  const pct = total > 0 ? (done / total * 100) : 0;
  document.getElementById('cbz-file-bar').style.width = pct + '%';
  document.getElementById('cbz-file-progress-text').textContent = `${done} / ${total}`;
}

function cbzUpdatePageProgress(done, total) {
  const pct = total > 0 ? (done / total * 100) : 0;
  document.getElementById('cbz-page-bar').style.width = pct + '%';
  document.getElementById('cbz-page-progress-text').textContent = `${done} / ${total}`;
}

function cbzLog(msg, cls = '') {
  const box = document.getElementById('cbz-log-box');
  const line = document.createElement('span');
  line.className = 'log-line ' + cls;
  line.textContent = msg;
  box.appendChild(line);
  box.appendChild(document.createElement('br'));
  box.scrollTop = box.scrollHeight;
}

async function cbzCancel() {
  if (cbzEventSource) cbzEventSource.close();
  if (cbzSessionId) { await fetch(`/api/cbz/cancel/${cbzSessionId}`, {method: 'POST'}); cbzSessionId = null; }
  document.getElementById('cbz-process-btn').disabled = false;
  document.getElementById('cbz-cancel-btn').textContent = 'Cancel';
}

document.getElementById('cbz-source-input').addEventListener('keydown', e => { if (e.key === 'Enter') cbzScan(); });
</script>
</body>
</html>"""

# ── Routes: MangaDex download (unchanged from MDF 2.0) ────────────────────────

@app.route("/")
def index():
    return HTML

@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    body = request.json
    raw = body.get("url", "").strip()
    manga_id = extract_manga_id(raw)
    if not manga_id:
        return jsonify({"error": "Invalid MangaDex URL or ID"}), 400
    try:
        info = get_manga_info(manga_id)
        chapters = get_all_chapters(manga_id)
        chapters = deduplicate_chapters(chapters)
        gaps = detect_gaps(chapters)
        vol_groups = group_chapters_by_volume(chapters)
        volumes = []
        def vol_sort_key(v):
            try: return (0, float(v))
            except: return (1, v)
        for vk in sorted(vol_groups.keys(), key=vol_sort_key):
            chs = vol_groups[vk]
            volumes.append({"key": vk,
                            "label": f"Vol. {vk}" if vk != "unnumbered" else "Unnumbered",
                            "chapter_count": len(chs),
                            "page_count": sum(c["pages"] for c in chs)})
        return jsonify({"manga": info, "chapters": chapters,
                        "gaps": gaps, "volumes": volumes})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/download", methods=["POST"])
def api_download():
    body = request.json
    manga_id = body.get("manga_id")
    manga_title = body.get("manga_title", "unknown")
    chapter_ids = body.get("chapter_ids", [])
    output_dir = body.get("output_dir", DOWNLOAD_BASE)
    make_cbz = body.get("make_cbz", False)
    output_dir = os.path.expanduser(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    series_slug = slugify(manga_title)
    session_id = f"{manga_id}_{int(time.time())}"
    q = queue.Queue()
    download_sessions[session_id] = q
    def run():
        completed_chapters = []
        for ch in chapter_ids:
            if download_sessions.get(session_id) is None:
                break
            ch_q = queue.Queue()
            t = threading.Thread(target=download_chapter_worker,
                                 args=(session_id, ch, series_slug, output_dir, ch_q),
                                 daemon=True)
            t.start()
            while True:
                msg = ch_q.get()
                q.put(msg)
                if msg["type"] == "chapter_done":
                    ch_record = dict(ch)
                    ch_record["files"] = msg.get("files", [])
                    completed_chapters.append(ch_record)
                    break
                elif msg["type"] == "chapter_error":
                    break
            t.join()
            time.sleep(0.5)
        if make_cbz and download_sessions.get(session_id) is not None:
            build_cbz_worker(session_id, series_slug, completed_chapters, output_dir, q)
        else:
            q.put({"type": "all_done"})
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"session_id": session_id, "output_dir": output_dir})

@app.route("/api/stream/<session_id>")
def api_stream(session_id):
    q = download_sessions.get(session_id)
    if not q:
        return Response("Session not found", status=404)
    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["type"] == "all_done":
                    break
            except queue.Empty:
                yield "data: {\"type\": \"ping\"}\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/cancel/<session_id>", methods=["POST"])
def api_cancel(session_id):
    if session_id in download_sessions:
        download_sessions[session_id] = None
    return jsonify({"ok": True})

# ── Routes: CBZ Processor ─────────────────────────────────────────────────────

@app.route("/api/cbz/scan", methods=["POST"])
def api_cbz_scan():
    body = request.json or {}
    folder = (body.get("folder") or "").strip()
    if not folder:
        return jsonify({"error": "Folder path required"}), 400
    try:
        files = cbz_scan_folder(folder)
        return jsonify({"files": files, "folder": os.path.expanduser(folder)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/cbz/process", methods=["POST"])
def api_cbz_process():
    body = request.json or {}
    items = body.get("items") or []
    volume = body.get("volume", "")
    cover_path = body.get("cover_path", "")
    output_dir = body.get("output_dir", "")
    mode = body.get("mode", "cbz")
    if mode not in ("cbz", "folder"):
        return jsonify({"error": "Invalid mode"}), 400
    if not items:
        return jsonify({"error": "No items to process"}), 400
    if not output_dir:
        return jsonify({"error": "Output folder required"}), 400

    output_dir = os.path.expanduser(output_dir)
    session_id = f"cbz_{int(time.time()*1000)}"
    q = queue.Queue()
    cbz_sessions[session_id] = q
    t = threading.Thread(target=cbz_process_worker,
                         args=(session_id, items, volume, cover_path,
                               output_dir, mode, q),
                         daemon=True)
    t.start()
    return jsonify({"session_id": session_id, "output_dir": output_dir})

@app.route("/api/cbz/stream/<session_id>")
def api_cbz_stream(session_id):
    q = cbz_sessions.get(session_id)
    if not q:
        return Response("Session not found", status=404)
    def generate():
        while True:
            try:
                msg = q.get(timeout=30)
                yield f"data: {json.dumps(msg)}\n\n"
                if msg["type"] == "all_done":
                    break
            except queue.Empty:
                yield "data: {\"type\": \"ping\"}\n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/cbz/cancel/<session_id>", methods=["POST"])
def api_cbz_cancel(session_id):
    if session_id in cbz_sessions:
        cbz_sessions[session_id] = None
    return jsonify({"ok": True})

# ── Launch ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    PORT = 5000
    print(f"\n  ⚙  MangaFactory")
    print(f"  → Opening http://localhost:{PORT} in your browser...")
    print(f"  → Press Ctrl+C to quit\n")

    def _open_browser():
        time.sleep(1.2)
        webbrowser.open(f"http://localhost:{PORT}")

    threading.Thread(target=_open_browser, daemon=True).start()

    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)

    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
