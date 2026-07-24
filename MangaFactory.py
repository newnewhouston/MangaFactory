#!/usr/bin/env python3
"""
MangaFactory v1.9 — combined MangaDexFactory + CBZ Factory, single-file edition.

v1.9: drop-anything CBZ Processor. Every queued file is sniffed by magic
bytes before processing — a ".cbz" that is really a RAR (.cbr), 7-Zip
(.cb7) or tar (.cbt) archive is unpacked with the first available
extractor (WinRAR's UnRAR, 7-Zip, Windows' bundled tar.exe, or Python's
tarfile) and repacked into a genuine ZIP-backed CBZ automatically.
.cbr/.cb7/.cbt files are also accepted directly now. Files that can't be
fixed get a clear explanation (empty download, PDF, HTML error page,
corrupted zip…) instead of v1.8's bare "File is not a zip file".

Just run:  python MangaFactory.py

Tab 1 — Download:      Grab chapters from MangaDex, optionally package as CBZ.
Tab 2 — CBZ Processor: Take existing .cbz/.cbr/.cb7/.cbt files, loose image
                       files, or a .zip archive (new in v1.7), rename pages to
                       Chapter_XX_page_YYY.ext, insert a cover image
                       (000_cover.ext), and repackage as one volume CBZ
                       or a folder tree.

                       v1.7 zip handling: a dropped/uploaded .zip is
                       inspected — if it holds nested .cbz files each is
                       added to the queue as its own chapter; otherwise the
                       archive's images are treated as a single CBZ source.

Output layout:
    ~/Desktop/MangaFactory/
        Downloaded/   ← raw page images land here while a download runs
        exported/     ← finished .cbz volumes land here after packaging
        MDF/          ← MangaFactory's own working state (new in v1.6)
            .mdf_libs/      ← bundled Python deps (auto-installed)
            .mdf_uploads/   ← scratch space for the CBZ Processor tab,
                              auto-emptied after every successful
                              export (new in v1.6)

The Downloaded/ and exported/ subdirectories are created automatically.

No pip install needed — dependencies are fetched automatically on first
run into ~/Desktop/MangaFactory/MDF/.mdf_libs/.
"""

# ── Step 1: bootstrap dependencies before anything else imports ───────────────

import sys
import os
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))

# v1.6: MangaFactory's own working state (bundled deps + CBZ scratch space)
# now lives in a fixed location on the user's desktop, not next to the
# script. This keeps the script directory clean and means moving or
# copying the .py file no longer abandons (or duplicates) the deps cache.
_MDF_HOME = os.path.join(os.path.expanduser("~/Desktop/MangaFactory"), "MDF")
os.makedirs(_MDF_HOME, exist_ok=True)
_LIBS = os.path.join(_MDF_HOME, ".mdf_libs")
REQUIRED = {"flask": "flask>=3.0", "requests": "requests>=2.31",
            "cloudscraper": "cloudscraper>=1.2"}

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
DOWNLOAD_BASE = os.path.expanduser("~/Desktop/MangaFactory")

# v1.5 output layout: raw pages go in <base>/Downloaded/, finished CBZs
# in <base>/exported/. These names are exposed as constants so the rest
# of the script (and the README) can refer to a single source of truth.
DOWNLOAD_SUBDIR = "Downloaded"
EXPORT_SUBDIR = "exported"

def resolve_io_dirs(base):
    """Given a user-supplied MangaFactory base folder, return
    (downloaded_dir, exported_dir). Both are created on disk."""
    base = os.path.expanduser(base)
    os.makedirs(base, exist_ok=True)
    downloaded = os.path.join(base, DOWNLOAD_SUBDIR)
    exported = os.path.join(base, EXPORT_SUBDIR)
    os.makedirs(downloaded, exist_ok=True)
    os.makedirs(exported, exist_ok=True)
    return downloaded, exported

download_sessions = {}   # MDF download sessions
cbz_sessions = {}        # CBZ processing sessions

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
# v1.6: scratch dir for the CBZ Processor tab now lives under
# ~/Desktop/MangaFactory/MDF/. Its contents are wiped after every
# successful export (see _cleanup_uploads below) so leftover pages
# from one job never pollute the next.
CBZ_UPLOAD_DIR = os.path.join(_MDF_HOME, ".mdf_uploads")
_wc_scraper = None

def _cleanup_uploads():
    """Empty CBZ_UPLOAD_DIR while keeping the directory itself.

    v1.6: invoked after a CBZ export has been written to the
    `exported/` folder, so the user-supplied source files don't
    accumulate in MDF/.mdf_uploads/ between runs. Best-effort —
    swallows per-entry errors (a file can be locked on Windows if
    something else has it open) so a cleanup hiccup never tanks
    the export that just succeeded.
    """
    if not os.path.isdir(CBZ_UPLOAD_DIR):
        return
    try:
        for name in os.listdir(CBZ_UPLOAD_DIR):
            path = os.path.join(CBZ_UPLOAD_DIR, name)
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
            except Exception:
                pass
    except Exception:
        pass

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

# ── WeebCentral helpers ───────────────────────────────────────────────────────

def get_wc_scraper():
    global _wc_scraper
    if _wc_scraper is None:
        import cloudscraper
        _wc_scraper = cloudscraper.create_scraper()
    return _wc_scraper

def extract_wc_id(url_or_id):
    url_or_id = url_or_id.strip()
    m = re.search(r'weebcentral\.com/series/([A-Z0-9]+)', url_or_id)
    if m:
        return m.group(1)
    if re.match(r'^[A-Z0-9]{26}$', url_or_id):
        return url_or_id
    return None

def wc_get_manga_info(series_id):
    sc = get_wc_scraper()
    r = sc.get(f"https://weebcentral.com/series/{series_id}", timeout=15)
    r.raise_for_status()
    m = re.search(r'property="og:title" content="([^"]+)"', r.text)
    if m:
        title = re.sub(r'\s*\|\s*Weeb Central\s*$', '', m.group(1)).strip()
        return {"id": series_id, "title": title, "source": "weebcentral"}
    m = re.search(r'<h1[^>]*>([^<]+)</h1>', r.text)
    if m:
        return {"id": series_id, "title": m.group(1).strip(), "source": "weebcentral"}
    return {"id": series_id, "title": "Unknown", "source": "weebcentral"}

def wc_get_all_chapters(series_id):
    sc = get_wc_scraper()
    r = sc.get(
        f"https://weebcentral.com/series/{series_id}/full-chapter-list",
        headers={"HX-Request": "true"},
        timeout=30,
    )
    r.raise_for_status()
    entries = re.findall(
        r'href="https://weebcentral\.com/chapters/([A-Z0-9]+)"'
        r'.*?<span class="">([^<]+)</span>',
        r.text, re.DOTALL
    )
    chapters = []
    for ch_id, ch_label in reversed(entries):  # reverse so ch1 is first
        ch_label = ch_label.strip()
        num_m = re.search(r'[\d.]+$', ch_label)
        num_str = num_m.group(0) if num_m else ch_label
        chapters.append({
            "id": ch_id,
            "chapter": num_str,
            "title": "",
            "pages": 0,
            "volume": "",
            "source": "weebcentral",
        })
    return chapters

def wc_download_chapter_worker(session_id, chapter, series_slug, output_dir, q):
    ch_id = chapter["id"]
    ch_num = format_chapter_num(chapter["chapter"])
    prefix = f"{series_slug}_ch{ch_num}"
    try:
        sc = get_wc_scraper()
        r = sc.get(
            f"https://weebcentral.com/chapters/{ch_id}/images"
            f"?is_prev=False&current_page=1&reading_style=long_strip",
            headers={"HX-Request": "true"},
            timeout=20,
        )
        r.raise_for_status()
        img_urls = re.findall(
            r'src="(https://[^"]+\.(?:jpg|jpeg|png|webp|gif))"',
            r.text, re.IGNORECASE
        )
        img_urls = [u for u in img_urls if "broken_image" not in u]
        total = len(img_urls)
        q.put({"type": "chapter_start", "chapter": chapter["chapter"], "total": total})
        downloaded_files = []
        for i, img_url in enumerate(img_urls):
            ext = img_url.rsplit('.', 1)[-1].split('?')[0] if '.' in img_url else 'jpg'
            page_num = str(i + 1).zfill(3)
            out_name = f"{prefix}_{page_num}.{ext}"
            out_path = os.path.join(output_dir, out_name)
            if os.path.exists(out_path):
                q.put({"type": "page_done", "page": i + 1, "total": total,
                       "file": out_name, "skipped": True})
                downloaded_files.append(out_path)
                continue
            try:
                img_r = requests.get(img_url, timeout=20,
                                     headers={"Referer": "https://weebcentral.com/"})
                img_r.raise_for_status()
                with open(out_path, 'wb') as f:
                    f.write(img_r.content)
                downloaded_files.append(out_path)
                q.put({"type": "page_done", "page": i + 1, "total": total,
                       "file": out_name, "skipped": False})
            except Exception as e:
                q.put({"type": "page_error", "page": i + 1, "error": str(e)})
            time.sleep(0.2)
        q.put({"type": "chapter_done", "chapter": chapter["chapter"],
               "files": downloaded_files})
    except Exception as e:
        q.put({"type": "chapter_error", "chapter": chapter["chapter"],
               "error": str(e)})

# ── MangaDex download worker ──────────────────────────────────────────────────

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
    q.put({"type": "cbz_start", "total": total_vols, "unit": "volume"})
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
            zipped_files = []
            with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_STORED) as zf:
                for fp in files:
                    if os.path.exists(fp):
                        zf.write(fp, os.path.basename(fp))
                        zipped_files.append(fp)
            # CBZ built successfully — remove the raw image files that went
            # into it so the output folder only contains the packaged volumes.
            removed = 0
            for fp in zipped_files:
                try:
                    os.remove(fp)
                    removed += 1
                except OSError:
                    pass
            q.put({"type": "cbz_done", "vol": vol_key, "cbz": cbz_name,
                   "index": i + 1, "total": total_vols,
                   "raw_removed": removed})
        except Exception as e:
            q.put({"type": "cbz_error", "vol": vol_key, "error": str(e)})
    q.put({"type": "all_done"})

def build_cbz_per_chapter_worker(session_id, series_slug, completed_chapters,
                                 output_dir, q):
    """v1.7: package each downloaded chapter into its own standalone .cbz.

    Mirrors build_cbz_worker's event protocol (cbz_start / cbz_building /
    cbz_done / cbz_error / all_done) but emits one entry per chapter instead
    of per volume, so the same Download-tab progress UI renders it unchanged.
    Raw page images are removed after each chapter is packaged.
    """
    items = [ch for ch in completed_chapters if ch.get("files")]

    def ch_sort_key(ch):
        try:
            return (0, float(ch.get("chapter")))
        except (TypeError, ValueError):
            return (1, str(ch.get("chapter") or ""))

    items = sorted(items, key=ch_sort_key)
    total = len(items)
    q.put({"type": "cbz_start", "total": total, "unit": "chapter"})
    for i, ch in enumerate(items):
        if download_sessions.get(session_id) is None:
            break
        files = sorted(ch.get("files", []))
        if not files:
            continue
        ch_raw = ch.get("chapter")
        numbered = ch_raw not in (None, "")
        ch_num = format_chapter_num(ch_raw)
        # key doubles as the progress-row id and label source on the client.
        key = re.sub(r'[^\w.]', '_', str(ch_raw)) if numbered else f"unnumbered_{i + 1}"
        cbz_name = (f"{series_slug}_ch{ch_num}.cbz" if numbered
                    else f"{series_slug}_ch_unnumbered_{i + 1}.cbz")
        cbz_path = os.path.join(output_dir, cbz_name)
        q.put({"type": "cbz_building", "vol": key, "cbz": cbz_name,
               "file_count": len(files)})
        try:
            zipped_files = []
            with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_STORED) as zf:
                for fp in files:
                    if os.path.exists(fp):
                        zf.write(fp, os.path.basename(fp))
                        zipped_files.append(fp)
            removed = 0
            for fp in zipped_files:
                try:
                    os.remove(fp)
                    removed += 1
                except OSError:
                    pass
            q.put({"type": "cbz_done", "vol": key, "cbz": cbz_name,
                   "index": i + 1, "total": total, "raw_removed": removed})
        except Exception as e:
            q.put({"type": "cbz_error", "vol": key, "error": str(e)})
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
    name = re.sub(r'\.(cbz|cbr|cb7|cbt|zip)$', '', filename, flags=re.IGNORECASE)
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

def cbz_sniff_format(path):
    """v1.9: identify what a '.cbz' file actually is, by magic bytes.

    Plenty of files in the wild carry a .cbz extension but are really RAR
    (.cbr), 7-Zip (.cb7) or tar (.cbt) archives — those made zipfile choke
    with the bare 'File is not a zip file' error in earlier versions.
    """
    try:
        with open(path, 'rb') as fh:
            head = fh.read(8)
            fh.seek(257)
            tar_magic = fh.read(5)
    except OSError:
        return "unreadable"
    if not head:
        return "empty"
    if head[:4] in (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08'):
        return "zip"
    if head.startswith(b'Rar!\x1a\x07'):
        return "rar"
    if head.startswith(b'7z\xbc\xaf\x27\x1c'):
        return "7z"
    if head.startswith(b'\x1f\x8b'):
        return "gzip"
    if head.startswith(b'%PDF'):
        return "pdf"
    if head.lstrip()[:1] == b'<':
        return "html"
    if tar_magic == b'ustar':
        return "tar"
    return "unknown"

_CBZ_FMT_LABEL = {
    "rar":  "a RAR archive (a .cbr renamed to .cbz)",
    "7z":   "a 7-Zip archive (a .cb7 renamed to .cbz)",
    "tar":  "a tar archive (a .cbt renamed to .cbz)",
    "gzip": "a gzip archive",
    "zip":  "a corrupted or truncated ZIP",
    "empty": "an empty file (0 bytes) — the download probably failed",
    "pdf":  "a PDF, not a comic archive",
    "html": "an HTML page — the source site probably served an error page "
            "instead of the archive",
    "unknown": "not a recognizable archive format",
    "unreadable": "unreadable (locked or missing)",
}

def _archive_extractors(fmt):
    """Ordered (label, exe) external extractors able to unpack `fmt`.

    UnRAR only speaks RAR; 7-Zip and bsdtar (tar.exe ships with Windows
    10+) read nearly everything, so they back every format as a fallback
    chain. Only tools actually present on this machine are returned.
    """
    found = []
    def probe(label, names, guesses):
        for n in names:
            p = shutil.which(n)
            if p:
                found.append((label, p))
                return
        for g in guesses:
            if os.path.isfile(g):
                found.append((label, g))
                return
    if fmt == "rar":
        probe("unrar", ["unrar"],
              [r"C:\Program Files\WinRAR\UnRAR.exe",
               r"C:\Program Files (x86)\WinRAR\UnRAR.exe"])
    probe("7z", ["7z", "7za"],
          [r"C:\Program Files\7-Zip\7z.exe",
           r"C:\Program Files (x86)\7-Zip\7z.exe"])
    probe("tar", ["tar", "bsdtar"], [r"C:\Windows\System32\tar.exe"])
    return found

def _run_extractor(label, exe, archive, out_dir):
    if label == "unrar":
        argv = [exe, "x", "-y", "-inul", archive, out_dir + os.sep]
    elif label == "7z":
        argv = [exe, "x", "-y", "-o" + out_dir, archive]
    else:  # bsdtar — auto-detects rar/7z/tar/zip via libarchive
        argv = [exe, "-xf", archive, "-C", out_dir]
    try:
        subprocess.run(argv, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=600)
    except Exception:
        pass  # success is judged by what landed in out_dir, not exit code

def _repack_dir_as_cbz(src_dir, dest_cbz):
    """Zip every image under src_dir (recursive) into dest_cbz, flattened.
    Returns the page count; raises ValueError if extraction yielded no
    images (the caller treats that as 'this extractor didn't work')."""
    images = []
    for root, dirs, files_ in os.walk(src_dir):
        dirs.sort(key=cbz_sort_key)
        for name in sorted(files_, key=cbz_sort_key):
            ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
            if ext in IMAGE_EXTS:
                images.append(os.path.join(root, name))
    if not images:
        raise ValueError("no images inside")
    with zipfile.ZipFile(dest_cbz, 'w', zipfile.ZIP_STORED) as zf:
        used = set()
        for fp in images:
            arc = os.path.relpath(fp, src_dir).replace(os.sep, '_')
            if arc in used:  # flattening collision — extremely rare
                b, e = os.path.splitext(arc)
                n = 2
                while f"{b}_{n}{e}" in used:
                    n += 1
                arc = f"{b}_{n}{e}"
            used.add(arc)
            zf.write(fp, arc)
    return len(images)

def cbz_ensure_zip(path):
    """v1.9: guarantee `path` is something zipfile can open.

    Returns (usable_path, note). A file that already is a real ZIP passes
    through untouched (note None). Otherwise its actual format is sniffed
    and the archive is unpacked with the first extractor that works, its
    images repacked into a genuine ZIP-backed .cbz in MDF/.mdf_uploads/,
    and that converted path returned. Raises ValueError with a
    human-readable explanation when nothing works — never the bare
    'File is not a zip file' that v1.8 surfaced.
    """
    try:
        with zipfile.ZipFile(path, 'r'):
            return path, None
    except (zipfile.BadZipFile, OSError):
        pass
    fmt = cbz_sniff_format(path)
    label = _CBZ_FMT_LABEL.get(fmt, _CBZ_FMT_LABEL["unknown"])
    if fmt in ("empty", "pdf", "html", "unreadable"):
        raise ValueError(f"this file is {label}")
    os.makedirs(CBZ_UPLOAD_DIR, exist_ok=True)
    ts = int(time.time() * 1000)
    base = os.path.splitext(os.path.basename(path))[0]
    extract_dir = os.path.join(CBZ_UPLOAD_DIR, f"_convert_{ts}")
    dest_cbz = os.path.join(CBZ_UPLOAD_DIR, f"{base}_converted_{ts}.cbz")

    def _attempt(runner, tool_name):
        shutil.rmtree(extract_dir, ignore_errors=True)
        os.makedirs(extract_dir, exist_ok=True)
        runner()
        try:
            pages = _repack_dir_as_cbz(extract_dir, dest_cbz)
        except ValueError:
            return None
        finally:
            shutil.rmtree(extract_dir, ignore_errors=True)
        return (f"was {label} — auto-converted to a real CBZ "
                f"({pages} pages, via {tool_name})")

    tried = []
    if fmt in ("tar", "gzip"):
        import tarfile
        def _py_tar():
            try:
                with tarfile.open(path, 'r:*') as tf:
                    try:
                        tf.extractall(extract_dir, filter='data')
                    except TypeError:  # Python < 3.12: no filter kwarg
                        tf.extractall(extract_dir)
            except Exception:
                pass
        note = _attempt(_py_tar, "tarfile")
        if note:
            return dest_cbz, note
        tried.append("tarfile")
    for tool, exe in _archive_extractors(fmt):
        note = _attempt(lambda: _run_extractor(tool, exe, path, extract_dir),
                        tool)
        if note:
            return dest_cbz, note
        tried.append(tool)
    if tried:
        raise ValueError(f"this file is {label}, and conversion failed "
                         f"(tried: {', '.join(tried)}) — the archive may be "
                         f"corrupted; try re-downloading it")
    raise ValueError(f"this file is {label} and no extractor is available — "
                     f"install 7-Zip or WinRAR, then retry")

def cbz_volume_folder_name(volume_value):
    v = (volume_value or "").strip()
    return f"Volume_{v}" if v else "New Volume"

def cbz_output_base_name(volume_value, items):
    """Decide the output CBZ/folder name → returns (display, fs_safe).

    Priority:
      1. If a Volume Number is supplied → 'Volume_{v}' (unchanged behaviour).
      2. Else, if the queued items carry chapter numbers, name the output
         after the chapter(s): a single chapter → just that number
         (e.g. '7' → '7.cbz'); several distinct chapters → 'first-last'.
      3. Else → the generic 'New Volume' fallback.

    v1.7: rule (2) is new. Previously an empty Volume Number always
    produced 'New Volume' even when the chapter number was known.
    """
    v = (volume_value or "").strip()
    if v:
        name = f"Volume_{v}"
        return name, name.replace(' ', '_')
    chapters = []
    for it in (items or []):
        c = (it.get("chapter") or "").strip()
        if c and c not in chapters:
            chapters.append(c)
    if chapters:
        chapters.sort(key=cbz_sort_key)
        base = chapters[0] if len(chapters) == 1 else f"{chapters[0]}-{chapters[-1]}"
        safe = re.sub(r'[^\w\s.\-]', '_', base).strip().replace(' ', '_')
        return base, (safe or "New_Volume")
    return "New Volume", "New_Volume"

def cbz_scan_folder(folder_path):
    """Scan a folder for comic archives, return list sorted naturally with detected chapter numbers."""
    folder_path = os.path.expanduser(folder_path)
    if not os.path.isdir(folder_path):
        raise ValueError(f"Folder not found: {folder_path}")
    files = []
    for entry in sorted(os.listdir(folder_path), key=cbz_sort_key):
        full = os.path.join(folder_path, entry)
        if os.path.isfile(full) and entry.lower().endswith(
                ('.cbz', '.cbr', '.cb7', '.cbt')):
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
        # v1.5: in single-CBZ mode the finished .cbz is routed into
        # <output_dir>/exported/. Folder-tree mode is unchanged — its output
        # is a directory of renamed images, not a CBZ, so the "exported"
        # rule (which is about CBZ outputs) doesn't apply.
        if mode == "cbz":
            cbz_out_root = os.path.join(output_dir, EXPORT_SUBDIR)
            os.makedirs(cbz_out_root, exist_ok=True)
        else:
            cbz_out_root = output_dir
        vol_folder, vol_fs = cbz_output_base_name(volume_value, items)

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
            display = item.get("name") or os.path.basename(item["path"])
            try:
                # v1.9: normalize disguised .cbr/.cb7/.cbt (and corrupted
                # zips) into real ZIP CBZs before the pipeline opens them.
                real_path, note = cbz_ensure_zip(item["path"])
                if note:
                    q.put({"type": "log", "level": "warn",
                           "text": f"↻ {display} {note}"})
                item["path"] = real_path
                with zipfile.ZipFile(real_path, 'r') as zf:
                    images = cbz_list_image_entries(zf)
                    total_pages += len(images)
                    item_info.append((item, images))
            except Exception as e:
                q.put({"type": "file_error", "file": display, "error": str(e)})
                item_info.append((item, None))
        q.put({"type": "pages_total", "total": total_pages})

        pages_done = 0

        # Prepare output container
        if mode == "cbz":
            out_cbz_path = os.path.join(cbz_out_root, f"{vol_fs}.cbz")
            out_zip = zipfile.ZipFile(out_cbz_path, 'w', zipfile.ZIP_STORED)
            q.put({"type": "log", "level": "info",
                   "text": f"Output CBZ: {out_cbz_path}"})
            if cover_entry:
                out_zip.write(cover_entry[1], cover_entry[0])
                q.put({"type": "log", "level": "ok",
                       "text": f"+ cover: {cover_entry[0]}"})
        else:
            # folder mode
            vol_dir = os.path.join(cbz_out_root, vol_fs)
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
                file_name = item.get("name") or os.path.basename(item["path"])
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
            # v1.6: a fresh .cbz has just been written into the
            # `exported/` folder, so the user-uploaded source files in
            # MDF/.mdf_uploads/ have served their purpose. Wipe them
            # before signalling all_done so the directory is empty by
            # the time the UI re-enables for the next job.
            _cleanup_uploads()
            q.put({"type": "all_done",
                   "output_path": os.path.join(cbz_out_root, f"{vol_fs}.cbz"),
                   "mode": mode})
        else:
            # Folder-tree mode also consumed the uploads, even though
            # its output isn't a .cbz in `exported/`. Clean up too so
            # the behaviour is consistent across both processor modes.
            _cleanup_uploads()
            q.put({"type": "all_done",
                   "output_path": os.path.join(cbz_out_root, vol_fs),
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
<title>MangaFactory v1.9</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #07080c; --surface: #0e1016; --surface2: #161925; --border: #1f2332;
    --border-hi: #2b3045;
    --accent: #ff5a2e; --accent2: #ffb066; --accent-soft: rgba(255,90,46,0.10);
    --text: #eef0f6; --muted: #7e8499;
    --success: #34d399; --warn: #fbbf24; --danger: #f87171;
    --mono: 'JetBrains Mono', monospace; --sans: 'Space Grotesk', sans-serif;
    --r-lg: 16px; --r-md: 12px; --r-sm: 9px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--sans); min-height: 100vh; overflow-x: hidden; -webkit-font-smoothing: antialiased; }
  body::before {
    content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 0;
    background:
      radial-gradient(640px 320px at 12% -4%, rgba(255,90,46,0.07), transparent 70%),
      radial-gradient(900px 420px at 88% -8%, rgba(94,118,255,0.05), transparent 70%);
  }
  .container { position: relative; z-index: 1; max-width: 900px; margin: 0 auto; padding: 44px 24px 88px; }

  header { display: flex; align-items: center; gap: 16px; margin-bottom: 26px; padding-bottom: 22px; border-bottom: 1px solid var(--border); }
  .logo-mark { width: 46px; height: 46px; border-radius: 14px; background: linear-gradient(135deg, #ff7c52, #e8441a); display: flex; align-items: center; justify-content: center; font-size: 17px; font-weight: 700; letter-spacing: -0.5px; color: #fff; flex-shrink: 0; box-shadow: 0 6px 18px rgba(255,90,46,0.35), inset 0 1px 0 rgba(255,255,255,0.25); }
  h1 { font-size: 27px; font-weight: 700; letter-spacing: -0.7px; line-height: 1; }
  h1 span { color: var(--accent); }
  .tagline { font-size: 13px; color: var(--muted); margin-top: 4px; letter-spacing: 0.2px; }
  .version { font-family: var(--mono); font-size: 11px; color: var(--muted); margin-left: auto; background: var(--surface); border: 1px solid var(--border); border-radius: 999px; padding: 5px 12px; }

  /* Tabs — segmented pill */
  .tabs { display: flex; width: max-content; gap: 4px; margin-bottom: 24px; background: var(--surface); border: 1px solid var(--border); border-radius: 999px; padding: 4px; }
  .tab { background: transparent; border: none; color: var(--muted); font-family: var(--sans); font-size: 14px; font-weight: 600; letter-spacing: 0.1px; padding: 9px 22px; cursor: pointer; border-radius: 999px; transition: color 0.15s, background 0.15s, box-shadow 0.15s; }
  .tab:hover { color: var(--text); }
  .tab.active { color: #fff; background: linear-gradient(135deg, #ff7c52, #e8441a); box-shadow: 0 4px 14px rgba(255,90,46,0.30); }
  .tab-content { display: none; }
  .tab-content.active { display: block; animation: fadeIn 0.25s ease-out; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }

  /* Cards / inputs shared */
  .card { background: linear-gradient(180deg, rgba(255,255,255,0.025), rgba(255,255,255,0) 60%), var(--surface); border: 1px solid var(--border); border-radius: var(--r-lg); padding: 24px; margin-bottom: 16px; box-shadow: 0 14px 36px rgba(0,0,0,0.30); }
  .card-title { font-size: 11px; font-weight: 600; letter-spacing: 1.8px; text-transform: uppercase; color: var(--muted); margin-bottom: 16px; }
  .input-row { display: flex; gap: 10px; }
  input[type="text"] { flex: 1; background: var(--bg); border: 1px solid var(--border-hi); border-radius: var(--r-sm); color: var(--text); font-family: var(--mono); font-size: 13px; padding: 12px 16px; outline: none; transition: border-color 0.2s, box-shadow 0.2s; }
  input[type="text"]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  input[type="text"]::placeholder { color: var(--muted); opacity: 0.7; }
  .btn { background: linear-gradient(135deg, #ff7c52, #e8441a); color: #fff; border: none; border-radius: var(--r-sm); font-family: var(--sans); font-size: 14px; font-weight: 600; letter-spacing: 0.2px; padding: 12px 22px; cursor: pointer; transition: filter 0.15s, transform 0.12s, box-shadow 0.15s; white-space: nowrap; box-shadow: 0 4px 14px rgba(255,90,46,0.25); }
  .btn:hover { filter: brightness(1.08); transform: translateY(-1px); box-shadow: 0 7px 20px rgba(255,90,46,0.35); }
  .btn:active { transform: translateY(0) scale(0.98); }
  .btn:disabled { background: var(--surface2); color: var(--muted); cursor: not-allowed; transform: none; box-shadow: none; filter: none; }
  .btn-ghost { background: transparent; border: 1px solid var(--border-hi); color: var(--text); box-shadow: none; }
  .btn-ghost:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); filter: none; transform: none; box-shadow: none; }
  .btn-sm { font-size: 12px; padding: 7px 14px; border-radius: 8px; }
  .btn-success { background: linear-gradient(135deg, #3ddfa0, #1ca878); box-shadow: 0 4px 14px rgba(52,211,153,0.25); }
  .btn-success:hover { filter: brightness(1.06); box-shadow: 0 7px 20px rgba(52,211,153,0.32); }

  /* MDF-specific styles */
  #manga-info { display: none; align-items: center; gap: 16px; padding: 18px 22px; background: var(--surface2); border: 1px solid var(--border-hi); border-radius: var(--r-md); margin-bottom: 16px; }
  .manga-title-display { font-size: 18px; font-weight: 700; letter-spacing: -0.2px; }
  .manga-meta { font-family: var(--mono); font-size: 11px; color: var(--muted); margin-top: 3px; }
  .gap-alert { display: none; background: rgba(251,191,36,0.07); border: 1px solid rgba(251,191,36,0.28); border-radius: var(--r-md); padding: 14px 18px; margin-bottom: 16px; font-size: 13px; color: var(--warn); }
  .gap-alert strong { display: block; margin-bottom: 4px; font-size: 11px; letter-spacing: 1.4px; text-transform: uppercase; }
  #chapter-section { display: none; }
  .chapter-controls { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; }
  .chapter-controls .spacer { flex: 1; }
  .filter-input { background: var(--bg); border: 1px solid var(--border-hi); border-radius: 8px; color: var(--text); font-family: var(--mono); font-size: 12px; padding: 7px 12px; width: 170px; outline: none; transition: border-color 0.2s, box-shadow 0.2s; }
  .filter-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  .volume-header { display: flex; align-items: center; gap: 10px; padding: 9px 16px; background: var(--surface2); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 2; user-select: none; cursor: pointer; }
  .volume-header:hover { background: #1b1f2e; }
  .vol-label { font-family: var(--mono); font-size: 10px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: var(--accent2); }
  .vol-cbz-badge { font-family: var(--mono); font-size: 9px; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); border: 1px solid var(--border-hi); padding: 2px 8px; border-radius: 999px; }
  .vol-meta { font-family: var(--mono); font-size: 10px; color: var(--muted); margin-left: auto; }
  .chapter-list { max-height: 420px; overflow-y: auto; border: 1px solid var(--border); border-radius: var(--r-md); }
  .chapter-list::-webkit-scrollbar { width: 8px; }
  .chapter-list::-webkit-scrollbar-track { background: transparent; }
  .chapter-list::-webkit-scrollbar-thumb { background: var(--border-hi); border-radius: 99px; border: 2px solid var(--surface); }
  .chapter-row { display: flex; align-items: center; gap: 12px; padding: 10px 16px; border-bottom: 1px solid var(--border); cursor: pointer; transition: background 0.1s; user-select: none; }
  .chapter-row:last-child { border-bottom: none; }
  .chapter-row:hover { background: var(--surface2); }
  .chapter-row.selected { background: var(--accent-soft); }
  .chapter-row input[type="checkbox"] { accent-color: var(--accent); width: 15px; height: 15px; flex-shrink: 0; }
  .ch-num { font-family: var(--mono); font-size: 12px; font-weight: 500; color: var(--accent); width: 60px; flex-shrink: 0; }
  .ch-title { font-size: 13px; flex: 1; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ch-pages { font-family: var(--mono); font-size: 11px; color: var(--muted); flex-shrink: 0; }
  .ch-status { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--border-hi); }
  .ch-status.done { background: var(--success); box-shadow: 0 0 8px rgba(52,211,153,0.5); }
  .ch-status.error { background: var(--danger); }
  .ch-status.downloading { background: var(--warn); animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  #volume-summary { display: none; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  .vol-pill { background: var(--surface); border: 1px solid var(--border-hi); border-radius: 999px; padding: 6px 14px; font-family: var(--mono); font-size: 11px; color: var(--muted); }
  .vol-pill strong { color: var(--accent2); }
  .cbz-toggle-row { display: flex; align-items: center; gap: 12px; padding: 14px 0 16px; border-top: 1px solid var(--border); margin-top: 4px; }
  .toggle-wrap { position: relative; width: 42px; height: 24px; flex-shrink: 0; }
  .toggle-wrap input { opacity: 0; width: 0; height: 0; }
  .toggle-slider { position: absolute; inset: 0; background: var(--border-hi); border-radius: 99px; cursor: pointer; transition: background 0.2s; }
  .toggle-slider::before { content: ''; position: absolute; width: 18px; height: 18px; left: 3px; top: 3px; background: var(--muted); border-radius: 50%; transition: transform 0.2s, background 0.2s; }
  .toggle-wrap input:checked + .toggle-slider { background: var(--accent); }
  .toggle-wrap input:checked + .toggle-slider::before { transform: translateX(18px); background: #fff; }
  .cbz-label { font-size: 13px; font-weight: 600; }
  .cbz-sublabel { font-family: var(--mono); font-size: 11px; color: var(--muted); line-height: 1.6; }
  .cbz-progress-section { display: none; margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border); }
  .cbz-vol-list { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
  .cbz-vol-row { display: flex; align-items: center; gap: 10px; font-family: var(--mono); font-size: 11px; color: var(--muted); }
  .cbz-vol-icon { width: 14px; text-align: center; }
  .cbz-vol-icon.done { color: var(--success); }
  .cbz-vol-icon.building { color: var(--warn); animation: pulse 1s infinite; }
  .cbz-vol-icon.err { color: var(--danger); }
  .outdir-row { display: flex; gap: 10px; align-items: center; margin-bottom: 16px; }
  .outdir-label { font-family: var(--mono); font-size: 11px; color: var(--muted); white-space: nowrap; }
  #progress-section { display: none; }
  .overall-progress { margin-bottom: 18px; }
  .progress-label { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 11px; color: var(--muted); margin-bottom: 7px; }
  .progress-bar-wrap { background: var(--surface2); height: 8px; border-radius: 99px; overflow: hidden; }
  .progress-bar-fill { height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent2)); border-radius: 99px; transition: width 0.3s ease; width: 0%; }
  .current-chapter-info { font-family: var(--mono); font-size: 12px; color: var(--accent2); margin-bottom: 12px; }
  .log-box { background: #05060a; border: 1px solid var(--border); border-radius: var(--r-md); font-family: var(--mono); font-size: 11px; color: var(--muted); padding: 14px 16px; max-height: 160px; overflow-y: auto; line-height: 1.8; }
  .log-box::-webkit-scrollbar { width: 6px; }
  .log-box::-webkit-scrollbar-thumb { background: var(--border-hi); border-radius: 99px; }
  .log-line { display: block; }
  .log-line.ok { color: var(--success); }
  .log-line.err { color: var(--danger); }
  .log-line.info { color: var(--accent2); }
  .log-line.skip { color: var(--muted); }
  .log-line.warn { color: var(--warn); }
  .done-banner { display: none; background: rgba(52,211,153,0.08); border: 1px solid rgba(52,211,153,0.30); border-radius: var(--r-md); padding: 16px 20px; margin-top: 16px; font-weight: 600; color: var(--success); font-size: 14px; letter-spacing: 0.2px; }
  .done-actions { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
  #loading-spinner, #cbz-loading-spinner { display: none; font-family: var(--mono); font-size: 12px; color: var(--muted); margin-top: 10px; }
  .spinner { display: inline-block; animation: spin 1s linear infinite; margin-right: 6px; color: var(--accent); }
  @keyframes spin { to { transform: rotate(360deg); } }
  .selection-count { font-family: var(--mono); font-size: 11px; color: var(--muted); }

  /* CBZ Processor tab */
  #cbz-file-list { display: flex; flex-direction: column; gap: 8px; }
  .cbz-file-row { display: flex; align-items: center; gap: 12px; padding: 12px 16px; background: var(--surface2); border: 1px solid var(--border); border-radius: var(--r-md); transition: border-color 0.2s; }
  .cbz-file-row.status-active { border-color: var(--accent); }
  .cbz-file-row.status-done   { border-color: rgba(52,211,153,0.45); }
  .cbz-file-row.status-error  { border-color: rgba(248,113,113,0.5); }
  .cbz-file-icon { width: 34px; height: 34px; border-radius: 10px; background: var(--accent-soft); border: 1px solid rgba(255,90,46,0.30); color: var(--accent); display: flex; align-items: center; justify-content: center; font-size: 14px; flex-shrink: 0; }
  .cbz-file-details { flex: 1; min-width: 0; }
  .cbz-file-name { font-family: var(--mono); font-size: 12px; font-weight: 500; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .cbz-file-meta { font-family: var(--mono); font-size: 10px; color: var(--muted); margin-top: 2px; }
  .cbz-chapter-wrap { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  .cbz-badge { font-family: var(--mono); font-size: 9px; padding: 3px 9px; font-weight: 700; letter-spacing: 0.5px; border: 1px solid; border-radius: 999px; }
  .cbz-badge.auto   { background: rgba(52,211,153,0.10); color: var(--success); border-color: rgba(52,211,153,0.35); }
  .cbz-badge.manual { background: rgba(251,191,36,0.10); color: var(--warn);    border-color: rgba(251,191,36,0.35); }
  .cbz-chapter-input-group { display: flex; align-items: stretch; border: 1px solid var(--border-hi); border-radius: 8px; overflow: hidden; background: var(--bg); transition: border-color 0.2s, box-shadow 0.2s; }
  .cbz-chapter-input-group:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  .cbz-chapter-prefix { font-family: var(--mono); font-size: 11px; color: var(--muted); padding: 6px 8px; border-right: 1px solid var(--border); background: var(--surface); display: flex; align-items: center; }
  .cbz-chapter-input { border: none; background: transparent; color: var(--text); font-family: var(--mono); font-size: 12px; font-weight: 600; padding: 6px 8px; width: 80px; outline: none; }
  .cbz-file-remove { background: transparent; border: none; color: var(--muted); cursor: pointer; font-size: 14px; padding: 4px 8px; border-radius: 6px; flex-shrink: 0; transition: color 0.15s, background 0.15s; }
  .cbz-file-remove:hover { color: var(--danger); background: rgba(248,113,113,0.10); }
  .cbz-status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--border-hi); }
  .cbz-status-dot.active { background: var(--warn); animation: pulse 1s infinite; }
  .cbz-status-dot.done { background: var(--success); }
  .cbz-status-dot.error { background: var(--danger); }
  .cbz-settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 640px) { .cbz-settings-grid { grid-template-columns: 1fr; } }
  .cbz-field-label { font-family: var(--mono); font-size: 10px; letter-spacing: 1px; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; display: block; }
  .cbz-field-hint { font-family: var(--mono); font-size: 10px; color: var(--muted); margin-top: 6px; }
  .cbz-mode-row { display: flex; gap: 8px; margin-top: 10px; }
  .cbz-mode-btn { flex: 1; background: var(--bg); border: 1px solid var(--border-hi); border-radius: var(--r-sm); color: var(--muted); font-family: var(--sans); font-size: 13px; font-weight: 600; padding: 11px 10px; cursor: pointer; transition: all 0.15s; }
  .cbz-mode-btn:hover { color: var(--text); border-color: var(--accent2); }
  .cbz-mode-btn.active { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
  .empty-state { padding: 32px; text-align: center; color: var(--muted); font-family: var(--mono); font-size: 12px; border: 1px dashed var(--border-hi); border-radius: var(--r-md); }

  /* Drag & drop zone */
  .drop-zone { border: 1.5px dashed var(--border-hi); border-radius: var(--r-md); padding: 34px 24px; text-align: center; cursor: default; transition: border-color 0.2s, background 0.2s; }
  .drop-zone:hover { border-color: var(--accent); background: var(--accent-soft); }
  .drop-zone.drag-over { border-color: var(--accent); background: var(--accent-soft); }
  .drop-zone-icon { width: 46px; height: 46px; margin: 0 auto 12px; border-radius: 14px; background: var(--accent-soft); border: 1px solid rgba(255,90,46,0.30); color: var(--accent); display: flex; align-items: center; justify-content: center; font-size: 20px; }
  .drop-zone-label { font-size: 15px; font-weight: 600; color: var(--text); margin-bottom: 5px; }
  .drop-zone-hint { font-family: var(--mono); font-size: 11px; color: var(--muted); }
  .upload-status { font-family: var(--mono); font-size: 11px; color: var(--accent2); margin-top: 10px; min-height: 1em; }
</style>
</head>
<body>
<div class="container">
  <header>
    <div class="logo-mark">MF</div>
    <div>
      <h1>Manga<span>Factory</span></h1>
      <div class="tagline">Download · Process · Package</div>
    </div>
    <div class="version">v1.9</div>
  </header>

  <div class="tabs">
    <button class="tab active" data-tab="download">Download</button>
    <button class="tab" data-tab="cbz">CBZ Processor</button>
  </div>

  <!-- ─── DOWNLOAD TAB ──────────────────────────────────────────────────────── -->
  <div class="tab-content active" id="tab-download">

    <div class="card">
      <div class="card-title">Series URL or ID</div>
      <div class="input-row">
        <input type="text" id="url-input" placeholder="https://mangadex.org/title/... or https://weebcentral.com/series/..." />
        <button class="btn" id="fetch-btn" onclick="fetchSeries()">Fetch</button>
      </div>
      <div id="loading-spinner"><span class="spinner">◌</span> Fetching chapter list...</div>
    </div>

    <div class="card">
      <div class="card-title">Comix.to · Browser Grab</div>
      <div style="font-size:13px; color:var(--text); line-height:1.6; margin-bottom:14px;">
        comix.to encrypts its API, so MangaFactory can't fetch it server-side. Instead, pages are grabbed
        straight from your own logged-in browser. Drag the button to your bookmarks bar once, open any
        comix.to chapter, then click it — it packages every page into a
        <code style="color:var(--accent)">.cbz</code> and downloads it. Drop that file into the
        <b>CBZ Processor</b> tab to rename pages and finish the volume.
      </div>
      <div style="display:flex; flex-wrap:wrap; gap:8px 16px; font-family:var(--mono); font-size:11px; color:var(--muted); margin-bottom:14px;">
        <span><b style="color:var(--accent2)">1.</b> Drag → bookmarks bar</span>
        <span><b style="color:var(--accent2)">2.</b> Open a comix.to chapter</span>
        <span><b style="color:var(--accent2)">3.</b> Click the bookmark</span>
        <span><b style="color:var(--accent2)">4.</b> Drop the .cbz into CBZ Processor</span>
      </div>
      <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
        <a id="comix-bm" class="btn" href="#" draggable="true" style="text-decoration:none;">Comix → CBZ</a>
        <button class="btn btn-ghost btn-sm" id="comix-copy-bm">Copy bookmarklet</button>
        <button class="btn btn-ghost btn-sm" id="comix-copy-src">Copy console snippet</button>
        <span class="cbz-sublabel" id="comix-copied" style="color:var(--success);"></span>
      </div>
      <div class="cbz-sublabel" style="margin-top:10px;">
        Can't drag it? Click <b>Copy bookmarklet</b>, create a new bookmark, and paste it as the URL — or
        <b>Copy console snippet</b> and paste into the chapter page's DevTools console (F12).
      </div>
    </div>

    <script type="text/plain" id="comix-src">(async () => {
  let box = document.getElementById('mf-grab'); if (box) box.remove();
  box = document.createElement('div'); box.id = 'mf-grab';
  box.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;background:#0e1016;color:#eef0f6;font:13px/1.5 ui-monospace,monospace;padding:14px 16px;border:1px solid #ff5a2e;border-radius:12px;max-width:320px;box-shadow:0 6px 24px rgba(0,0,0,.5)';
  document.body.appendChild(box);
  const log = m => { box.innerHTML = '<b style="color:#ff5a2e">MangaFactory · Comix grab</b><br>' + m; };
  log('Starting…');
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const readerImg = [...document.querySelectorAll('img')].map(i => i.currentSrc || i.src).find(s => /\/i4\//.test(s));
  if (!readerImg) { log('No reader pages found.<br>Open a comix.to chapter, then run this again.'); return; }
  const m = readerImg.match(/^(https?:\/\/[^/]+\/i4\/[^/]+)\/(\d+)\.(\w+)/);
  if (!m) { log('Could not read the page image pattern.'); return; }
  const base = m[1], padW = m[2].length, ext = m[3];
  const url = n => base + '/' + String(n).padStart(padW, '0') + '.' + ext;
  const dens = {};
  (document.body.innerText.match(/\b\d{1,4}\s*\/\s*(\d{1,4})\b/g) || []).forEach(s => { const d = s.split('/')[1].trim(); dens[d] = (dens[d] || 0) + 1; });
  let total = 0, best = 0; for (const d in dens) if (dens[d] > best) { best = dens[d]; total = parseInt(d, 10); }
  const tryFetch = async n => {
    for (let t = 0; t < 3; t++) {
      try {
        const ac = new AbortController(); const id = setTimeout(() => ac.abort(), 8000);
        const r = await fetch(url(n), { mode: 'cors', cache: 'force-cache', signal: ac.signal }); clearTimeout(id);
        if (r.ok) return new Uint8Array(await r.arrayBuffer());
      } catch (e) {}
      await sleep(600 * (t + 1));
    }
    return null;
  };
  const files = []; let n = 0, misses = 0;
  while (n < 1000) {
    n++;
    log('Downloading page ' + n + (total ? ' / ' + total : '') + ' … (' + files.length + ' saved)');
    const d = await tryFetch(n);
    if (d) { files.push({ n, d }); misses = 0; }
    else { misses++; if (misses >= 2 && n >= total) break; }
    await sleep(350);
  }
  if (!files.length) { log('Could not fetch any pages.<br>Make sure a chapter is open, then retry.'); return; }
  const crcT = (() => { const t = new Uint32Array(256); for (let i = 0; i < 256; i++) { let c = i; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; t[i] = c >>> 0; } return t; })();
  const crc32 = u8 => { let c = 0xFFFFFFFF; for (let i = 0; i < u8.length; i++) c = crcT[(c ^ u8[i]) & 0xFF] ^ (c >>> 8); return (c ^ 0xFFFFFFFF) >>> 0; };
  const u16 = v => { const b = new Uint8Array(2); new DataView(b.buffer).setUint16(0, v, true); return b; };
  const u32 = v => { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, v >>> 0, true); return b; };
  const enc = new TextEncoder(); const chunks = [], central = []; let off = 0;
  for (const f of files) {
    const nm = enc.encode(String(f.n).padStart(3, '0') + '.' + ext), crc = crc32(f.d), sz = f.d.length;
    [u32(0x04034b50), u16(20), u16(0), u16(0), u16(0), u16(0), u32(crc), u32(sz), u32(sz), u16(nm.length), u16(0), nm].forEach(c => chunks.push(c));
    chunks.push(f.d);
    central.push([u32(0x02014b50), u16(20), u16(20), u16(0), u16(0), u16(0), u16(0), u32(crc), u32(sz), u32(sz), u16(nm.length), u16(0), u16(0), u16(0), u16(0), u32(0), u32(off), nm]);
    off += 30 + nm.length + sz;
  }
  const cdStart = off; const cd = []; let cdLen = 0;
  for (const c of central) c.forEach(x => { cd.push(x); cdLen += x.length; });
  const eocd = [u32(0x06054b50), u16(0), u16(0), u16(files.length), u16(files.length), u32(cdLen), u32(cdStart), u16(0)];
  const all = [...chunks, ...cd, ...eocd]; let tot = 0; all.forEach(c => tot += c.length);
  const zip = new Uint8Array(tot); let q = 0; all.forEach(c => { zip.set(c, q); q += c.length; });
  const title = (document.title || 'comix').replace(/\s*·.*$/, '').trim() || 'comix';
  let ch = ''; const cm = document.title.match(/ch\.?\s*([\d.]+)/i) || location.pathname.match(/chapter-([\d.]+)/i); if (cm) ch = cm[1];
  const safe = s => s.replace(/[\\/:*?"<>|]+/g, '').replace(/\s+/g, ' ').trim();
  const fname = (safe(title) || 'comix') + (ch ? ' - Ch ' + ch : '') + '.cbz';
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([zip], { type: 'application/zip' }));
  a.download = fname; document.body.appendChild(a); a.click(); a.remove();
  log('✓ Saved <b>' + fname + '</b><br>' + files.length + ' pages.<br>Now drop it into MangaFactory → CBZ Processor.');
  setTimeout(() => box.remove(), 15000);
})();</script>
    <script>
    (function () {
      var src = document.getElementById('comix-src').textContent;
      var bm = 'javascript:' + encodeURIComponent(src);
      var link = document.getElementById('comix-bm');
      link.href = bm;
      link.addEventListener('click', function (e) { e.preventDefault(); });
      function flash(t) { var el = document.getElementById('comix-copied'); el.textContent = t; setTimeout(function () { el.textContent = ''; }, 2500); }
      document.getElementById('comix-copy-bm').addEventListener('click', function () { navigator.clipboard.writeText(bm).then(function () { flash('✓ Bookmarklet copied'); }); });
      document.getElementById('comix-copy-src').addEventListener('click', function () { navigator.clipboard.writeText(src).then(function () { flash('✓ Snippet copied'); }); });
    })();
    </script>

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
          <input type="text" id="output-dir" style="flex:1; font-size:12px;" value="~/Desktop/MangaFactory" />
        </div>
        <div class="cbz-toggle-row" style="flex-direction:column; align-items:stretch; gap:8px;">
          <div class="cbz-label">Packaging</div>
          <div class="cbz-mode-row">
            <button class="cbz-mode-btn active" id="dl-mode-images" onclick="dlSetMode('images')">Images</button>
            <button class="cbz-mode-btn" id="dl-mode-volume" onclick="dlSetMode('volume')">One CBZ / Volume</button>
            <button class="cbz-mode-btn" id="dl-mode-chapter" onclick="dlSetMode('chapter')">One CBZ / Chapter</button>
          </div>
          <div class="cbz-sublabel" id="cbz-sublabel">Saves raw page images into Downloaded/ — no packaging.</div>
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
            <div class="progress-bar-fill" id="ch-bar" style="background: linear-gradient(90deg, var(--success), #6ee7b7);"></div>
          </div>
        </div>
        <div class="log-box" id="log-box"></div>
        <div class="cbz-progress-section" id="cbz-progress-section">
          <div class="card-title" style="margin-bottom:10px;">Building CBZ Volumes</div>
          <div class="cbz-vol-list" id="cbz-vol-list"></div>
        </div>
        <div class="done-banner" id="done-banner">✓ Done!</div>
        <div class="done-actions">
          <button class="btn btn-ghost btn-sm" id="cancel-btn" onclick="cancelDownload()">Cancel</button>
          <button class="btn btn-sm" id="send-to-cbz-btn" style="display:none" onclick="sendToCbzProcessor()">Send to CBZ Processor →</button>
        </div>
      </div>
    </div>

  </div>

  <!-- ─── CBZ PROCESSOR TAB ────────────────────────────────────────────────── -->
  <div class="tab-content" id="tab-cbz">

    <!-- Source folder elements kept hidden for the Download → CBZ handoff -->
    <input type="text" id="cbz-source-input" style="display:none" />
    <div id="cbz-loading-spinner" style="display:none; font-family:var(--mono); font-size:12px; color:var(--muted);"><span class="spinner">◌</span> Scanning...</div>

    <div class="card">
      <div class="card-title">Add Files</div>
      <div class="drop-zone" id="cbz-drop-zone" style="cursor:pointer">
        <div class="drop-zone-icon">↓</div>
        <div class="drop-zone-label">Drop files here or click to browse</div>
        <div class="drop-zone-hint">.cbz / .cbr / .cb7 / .cbt · a .zip archive · or image files (jpg, png, webp…) — non-zip archives are converted automatically</div>
      </div>
      <input type="file" id="cbz-file-picker" accept=".cbz,.cbr,.cb7,.cbt,.zip,.jpg,.jpeg,.png,.gif,.webp,.bmp" multiple style="display:none" />
      <div class="upload-status" id="cbz-upload-status"></div>
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
          <input type="text" id="cbz-output-dir" style="flex:1; font-size:12px;" value="~/Desktop/MangaFactory" />
        </div>
        <label class="cbz-field-label" style="margin-top: 10px;">Output Mode</label>
        <div class="cbz-mode-row">
          <button class="cbz-mode-btn active" id="cbz-mode-cbz" onclick="cbzSetMode('cbz')">Single CBZ</button>
          <button class="cbz-mode-btn" id="cbz-mode-folder" onclick="cbzSetMode('folder')">Folder Tree</button>
        </div>
        <div class="cbz-field-hint" id="cbz-mode-hint" style="margin-top: 8px;">Packages everything into a single Volume_XX.cbz file.</div>
        <button class="btn btn-success" id="cbz-process-btn" onclick="cbzStart()" style="margin-top: 18px;">Process Files</button>
      </div>
    </div>


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
            <div class="progress-bar-fill" id="cbz-file-bar" style="background: linear-gradient(90deg, var(--success), #6ee7b7);"></div>
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
let currentSource = 'mangadex';
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
    currentSource = data.manga.source || 'mangadex';
    document.getElementById('manga-title-display').textContent = data.manga.title;
    const srcBadge = currentSource === 'weebcentral' ? ' · WeebCentral' : ' · MangaDex';
    const volPart = allVolumes.length ? ` · ${allVolumes.length} volumes` : '';
    const pgPart = allChapters.reduce((s,c)=>s+c.pages,0);
    document.getElementById('manga-meta').textContent = `${allChapters.length} chapters${volPart}${pgPart ? ' · ' + pgPart + ' total pages' : ''}${srcBadge}`;
    dlSetMode(dlCbzMode);  // refresh the packaging sublabel for the current mode
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

let dlCbzMode = 'images';
function dlSetMode(mode) {
  dlCbzMode = mode;
  document.getElementById('dl-mode-images').classList.toggle('active', mode === 'images');
  document.getElementById('dl-mode-volume').classList.toggle('active', mode === 'volume');
  document.getElementById('dl-mode-chapter').classList.toggle('active', mode === 'chapter');
  const sub = document.getElementById('cbz-sublabel');
  if (sub) sub.textContent =
    mode === 'images' ? 'Saves raw page images into Downloaded/ — no packaging.' :
    mode === 'volume' ? 'Groups selected chapters by volume → one .cbz per volume in exported/ (raw images removed after). Sources without volume info yield a single combined .cbz.' :
    'Packages each selected chapter into its own standalone .cbz in exported/ (raw images removed after each chapter).';
}

async function startDownload() {
  const selected = getSelectedChapters();
  if (!selected.length) { alert('Select at least one chapter.'); return; }
  const outputDir = document.getElementById('output-dir').value.trim();
  const mode = dlCbzMode;
  const makeCbz = mode !== 'images';
  document.getElementById('dl-btn').disabled = true;
  document.getElementById('progress-section').style.display = 'block';
  document.getElementById('done-banner').style.display = 'none';
  document.getElementById('send-to-cbz-btn').style.display = 'none';
  document.getElementById('log-box').innerHTML = '';
  document.getElementById('cbz-progress-section').style.display = 'none';
  document.getElementById('cbz-vol-list').innerHTML = '';
  totalChs = selected.length; doneChs = 0;
  updateChProgress(0, totalChs); updatePageProgress(0, 0);
  const res = await fetch('/api/download', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ manga_id: mangaInfo.id, manga_title: mangaInfo.title, chapter_ids: selected, output_dir: outputDir, make_cbz: makeCbz, cbz_mode: mode, source: currentSource }) });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }
  sessionId = data.session_id;
  lastDownloadContext = {
    output_dir: data.output_dir,
    download_dir: data.download_dir,
    export_dir: data.export_dir,
    make_cbz: data.make_cbz,
    title: mangaInfo.title,
  };
  log(`Base folder:  ${data.output_dir}`, 'info');
  log(`Downloaded → ${data.download_dir}`, 'info');
  if (makeCbz) {
    log(`Exported → ${data.export_dir}`, 'info');
    log(`CBZ packaging enabled (one per ${mode}) — files build into exported/ after download.`, 'info');
  }
  eventSource = new EventSource(`/api/stream/${sessionId}`);
  let curChTotal = 0, curChDone = 0, cbzUnit = 'volume';
  const cbzLabel = (vol) => ('' + vol).indexOf('unnumbered') === 0 ? 'Unnumbered' : (cbzUnit === 'chapter' ? 'Ch. ' : 'Vol. ') + vol;
  eventSource.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'ping') return;
    if (msg.type === 'chapter_start') { curChTotal = msg.total; curChDone = 0; document.getElementById('current-chapter-info').textContent = `Downloading Chapter ${msg.chapter} (${msg.total} pages)`; log(`─── Chapter ${msg.chapter} ───`, 'info'); updatePageProgress(0, msg.total); }
    if (msg.type === 'page_done') { curChDone++; log(msg.skipped ? `  skip  ${msg.file}` : `  ✓  ${msg.file}`, msg.skipped ? 'skip' : 'ok'); updatePageProgress(curChDone, curChTotal); }
    if (msg.type === 'page_error') { log(`  ✗  page ${msg.page}: ${msg.error}`, 'err'); }
    if (msg.type === 'chapter_done') { doneChs++; updateChProgress(doneChs, totalChs); const s = document.getElementById(`status-${getChIdByNum(msg.chapter)}`); if (s) s.className = 'ch-status done'; }
    if (msg.type === 'chapter_error') { log(`  ✗  Chapter ${msg.chapter} failed: ${msg.error}`, 'err'); doneChs++; updateChProgress(doneChs, totalChs); }
    if (msg.type === 'cbz_start') { cbzUnit = msg.unit || 'volume'; document.getElementById('cbz-progress-section').style.display = 'block'; document.getElementById('current-chapter-info').textContent = `Building ${msg.total} CBZ ${cbzUnit}${msg.total > 1 ? 's' : ''}...`; log(`─── Packaging ${msg.total} CBZ ${cbzUnit}(s) ───`, 'info'); }
    if (msg.type === 'cbz_building') { addCbzRow(msg.vol, `building-${msg.vol}`, '⧗', 'building', `${cbzLabel(msg.vol)} → ${msg.cbz} (${msg.file_count} files)`); }
    if (msg.type === 'cbz_done') { const cleanup = msg.raw_removed ? ` (cleaned up ${msg.raw_removed} raw file${msg.raw_removed === 1 ? '' : 's'})` : ''; updateCbzRow(`building-${msg.vol}`, '✓', 'done', `${cbzLabel(msg.vol)} → ${msg.cbz}${cleanup}`); log(`  ✓  ${msg.cbz}${cleanup}`, 'ok'); }
    if (msg.type === 'cbz_error') { updateCbzRow(`building-${msg.vol}`, '✗', 'err', `${cbzLabel(msg.vol)} failed: ${msg.error}`); log(`  ✗  ${cbzLabel(msg.vol)}: ${msg.error}`, 'err'); }
    if (msg.type === 'all_done') {
      eventSource.close();
      const cbzOn = dlCbzMode !== 'images';
      document.getElementById('done-banner').style.display = 'block';
      document.getElementById('done-banner').textContent = cbzOn ? `✓ All chapters downloaded and packaged (one CBZ per ${dlCbzMode}).` : '✓ All chapters downloaded successfully.';
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
  // v1.5 layout: raw images live in <base>/Downloaded/, packaged CBZs in
  // <base>/exported/. Source the CBZ processor at whichever subfolder the
  // user just produced. The processor's own output is the *base* folder —
  // its single-CBZ output will land in <base>/exported/ automatically.
  const ctx = lastDownloadContext;
  const source = ctx.make_cbz ? ctx.export_dir : ctx.download_dir;
  document.getElementById('cbz-source-input').value = source;
  document.getElementById('cbz-output-dir').value = ctx.output_dir;
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
    <div class="cbz-file-icon">▤</div>
    <div class="cbz-file-details">
      <div class="cbz-file-name" title="${item.name}">${item.name}</div>
      <div class="cbz-file-meta">${cbzFormatSize(item.size)}${item.note ? ' · ' + item.note : ''}</div>
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
    cbzUpdateVolumePreview();
  });
  el.querySelector('.cbz-file-remove').addEventListener('click', () => {
    cbzQueue = cbzQueue.filter(q => q.id !== item.id);
    el.remove();
    document.getElementById('cbz-file-count').textContent = `${cbzQueue.length} file${cbzQueue.length !== 1 ? 's' : ''}`;
    cbzUpdateVolumePreview();
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
  cbzUpdateVolumePreview();
}

function cbzClearFiles() {
  cbzQueue = [];
  cbzRenderFiles();
  document.getElementById('cbz-file-section').style.display = 'none';
  cbzUpdateVolumePreview();
}

// v1.7: when the Volume Number is blank, the output is named after the
// chapter number(s) in the queue instead of the generic "New Volume".
// Mirrors cbz_output_base_name() on the server.
function cbzComputeOutputName() {
  const v = document.getElementById('cbz-volume-input').value.trim();
  if (v) return `Volume_${v}`;
  const chapters = [...new Set(cbzQueue.map(it => (it.chapter || '').trim()).filter(Boolean))];
  if (!chapters.length) return 'New Volume';
  chapters.sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  return chapters.length === 1 ? chapters[0] : `${chapters[0]}-${chapters[chapters.length - 1]}`;
}
function cbzUpdateVolumePreview() {
  document.getElementById('cbz-volume-preview').textContent = cbzComputeOutputName();
}
document.getElementById('cbz-volume-input').addEventListener('input', cbzUpdateVolumePreview);

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
    // v1.9: pass the display name through so progress rows and errors
    // reference the file the user dropped, not the converted temp path.
    items: cbzQueue.map(it => ({ path: it.path, chapter: it.chapter, name: it.name })),
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

/* ─────────────────────────────────────────────────────────────────────────
   Drag & drop / click-to-browse support for CBZ Processor
   ───────────────────────────────────────────────────────────────────────── */
(function () {
  const zone = document.getElementById('cbz-drop-zone');
  const picker = document.getElementById('cbz-file-picker');
  const statusEl = document.getElementById('cbz-upload-status');
  let dragCounter = 0;  // track nested enter/leave events

  const IMAGE_EXTS_JS = new Set(['jpg','jpeg','png','gif','webp','bmp']);
  function isImage(name) { return IMAGE_EXTS_JS.has(name.split('.').pop().toLowerCase()); }
  function isCbz(name)   { return /\.(cbz|cbr|cb7|cbt)$/i.test(name); }
  function isZip(name)   { return name.toLowerCase().endsWith('.zip'); }

  // Click → open file picker
  zone.addEventListener('click', () => picker.click());
  picker.addEventListener('change', () => {
    const all = [...picker.files];
    picker.value = '';  // reset so the same file can be re-selected later
    routeFiles(all);
  });

  zone.addEventListener('dragenter', e => {
    e.preventDefault();
    dragCounter++;
    zone.classList.add('drag-over');
  });
  zone.addEventListener('dragover', e => { e.preventDefault(); });
  zone.addEventListener('dragleave', () => {
    dragCounter--;
    if (dragCounter <= 0) { dragCounter = 0; zone.classList.remove('drag-over'); }
  });
  zone.addEventListener('drop', e => {
    e.preventDefault();
    dragCounter = 0;
    zone.classList.remove('drag-over');
    routeFiles([...e.dataTransfer.files]);
  });

  function routeFiles(all) {
    const cbzFiles   = all.filter(f => isCbz(f.name));
    const zipFiles   = all.filter(f => isZip(f.name));
    const imageFiles = all.filter(f => isImage(f.name));
    const unknown    = all.length - cbzFiles.length - zipFiles.length - imageFiles.length;
    if (!cbzFiles.length && !zipFiles.length && !imageFiles.length) {
      statusEl.textContent = 'No supported files detected (.cbz, .zip, or image files).';
      return;
    }
    if (unknown > 0) {
      statusEl.textContent = `Skipping ${unknown} unsupported file(s)...`;
    }
    if (cbzFiles.length)   cbzHandleCbzFiles(cbzFiles);
    if (zipFiles.length)   cbzHandleZipFiles(zipFiles);
    if (imageFiles.length) cbzHandleImageFiles(imageFiles);
  }

  async function cbzHandleCbzFiles(files) {
    statusEl.textContent = `Uploading ${files.length} CBZ file(s)...`;
    let added = 0, converted = 0;
    let lastError = '';
    for (const file of files) {
      statusEl.textContent = `Uploading ${file.name}...`;
      try {
        const fd = new FormData();
        fd.append('file', file);
        const res = await fetch('/api/cbz/upload', { method: 'POST', body: fd });
        const data = await res.json();
        if (data.error) { lastError = `✗ ${file.name}: ${data.error}`; statusEl.textContent = lastError; continue; }
        if (data.note) converted++;
        cbzAddToQueue(data);
        added++;
      } catch (err) {
        lastError = `✗ Failed to upload ${file.name}: ${err.message}`;
        statusEl.textContent = lastError;
      }
    }
    // v1.9: keep a failure on screen instead of overwriting it with the
    // summary — otherwise a rejected file vanishes after 3 seconds.
    const convNote = converted ? ` (${converted} auto-converted from RAR/7z/tar)` : '';
    if (added === files.length) {
      statusEl.textContent = `✓ Added ${added} of ${files.length} CBZ file(s).${convNote}`;
      setTimeout(() => { statusEl.textContent = ''; }, 5000);
    } else {
      statusEl.textContent = `Added ${added} of ${files.length}.${convNote} ${lastError}`;
    }
  }

  async function cbzHandleZipFiles(files) {
    // v1.7: a .zip is sent to the server, which either splits out nested
    // .cbz files (one queue item each) or treats the whole archive as a
    // single CBZ source. Either way the response is {items: [...]}.
    let added = 0;
    for (const file of files) {
      statusEl.textContent = `Uploading ${file.name}...`;
      try {
        const fd = new FormData();
        fd.append('file', file);
        const res = await fetch('/api/cbz/upload-zip', { method: 'POST', body: fd });
        const data = await res.json();
        if (data.error) { statusEl.textContent = `✗ ${file.name}: ${data.error}`; continue; }
        const items = data.items || [];
        items.forEach(cbzAddToQueue);
        added += items.length;
      } catch (err) {
        statusEl.textContent = `✗ Failed to upload ${file.name}: ${err.message}`;
      }
    }
    if (added) {
      statusEl.textContent = `✓ Added ${added} item(s) from ${files.length} zip(s).`;
      setTimeout(() => { statusEl.textContent = ''; }, 3000);
    }
  }

  async function cbzHandleImageFiles(files) {
    statusEl.textContent = `Bundling ${files.length} image(s) into a CBZ...`;
    try {
      const fd = new FormData();
      files.forEach(f => fd.append('files', f));
      const res = await fetch('/api/cbz/upload-images', { method: 'POST', body: fd });
      const data = await res.json();
      if (data.error) { statusEl.textContent = `✗ ${data.error}`; return; }
      cbzAddToQueue(data);
      statusEl.textContent = `✓ Bundled ${files.length} image(s) — set the chapter number below.`;
      setTimeout(() => { statusEl.textContent = ''; }, 4000);
    } catch (err) {
      statusEl.textContent = `✗ Failed to bundle images: ${err.message}`;
    }
  }

  function cbzAddToQueue(data) {
    const item = { id: cbzNextId++, ...data, chapter: data.detected_chapter };
    cbzQueue.push(item);
    document.getElementById('cbz-file-section').style.display = 'block';
    document.getElementById('cbz-file-list').appendChild(cbzRenderRow(item));
    document.getElementById('cbz-file-count').textContent =
      `${cbzQueue.length} file${cbzQueue.length !== 1 ? 's' : ''}`;
    cbzUpdateVolumePreview();
  }
})();
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

    # ── WeebCentral ───────────────────────────────────────────────────────────
    if "weebcentral.com" in raw or re.match(r'^[A-Z0-9]{26}$', raw):
        series_id = extract_wc_id(raw)
        if not series_id:
            return jsonify({"error": "Invalid WeebCentral URL or ID"}), 400
        try:
            info = wc_get_manga_info(series_id)
            chapters = wc_get_all_chapters(series_id)
            gaps = detect_gaps(chapters)
            return jsonify({"manga": info, "chapters": chapters,
                            "gaps": gaps, "volumes": []})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── MangaDex ──────────────────────────────────────────────────────────────
    manga_id = extract_manga_id(raw)
    if not manga_id:
        return jsonify({"error": "Invalid MangaDex or WeebCentral URL / ID"}), 400
    try:
        info = get_manga_info(manga_id)
        info["source"] = "mangadex"
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
    # v1.7: packaging mode — "images" (no CBZ), "volume" (one CBZ per volume,
    # the original behaviour) or "chapter" (one standalone CBZ per chapter).
    # Fall back to the legacy make_cbz boolean for older callers.
    cbz_mode = body.get("cbz_mode")
    if cbz_mode not in ("images", "volume", "chapter"):
        cbz_mode = "volume" if body.get("make_cbz", False) else "images"
    make_cbz = cbz_mode != "images"
    source = body.get("source", "mangadex")
    # v1.5: raw pages always go in <base>/Downloaded/, finished CBZs in
    # <base>/exported/. The user-supplied output_dir is the *base* folder.
    download_dir, export_dir = resolve_io_dirs(output_dir)
    base_dir = os.path.expanduser(output_dir)
    series_slug = slugify(manga_title)
    session_id = f"{manga_id}_{int(time.time())}"
    q = queue.Queue()
    download_sessions[session_id] = q
    worker_fn = wc_download_chapter_worker if source == "weebcentral" else download_chapter_worker
    def run():
        completed_chapters = []
        for ch in chapter_ids:
            if download_sessions.get(session_id) is None:
                break
            ch_q = queue.Queue()
            t = threading.Thread(target=worker_fn,
                                 args=(session_id, ch, series_slug, download_dir, ch_q),
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
        alive = download_sessions.get(session_id) is not None
        if alive and cbz_mode == "volume":
            build_cbz_worker(session_id, series_slug, completed_chapters, export_dir, q)
        elif alive and cbz_mode == "chapter":
            build_cbz_per_chapter_worker(session_id, series_slug, completed_chapters, export_dir, q)
        else:
            q.put({"type": "all_done"})
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"session_id": session_id, "output_dir": base_dir,
                    "download_dir": download_dir, "export_dir": export_dir,
                    "make_cbz": make_cbz, "cbz_mode": cbz_mode})

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

# ── Routes: CBZ Processor ────────────────────────────────────────────

@app.route("/api/cbz/upload", methods=["POST"])
def api_cbz_upload():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files['file']
    orig_name = f.filename or ""
    # v1.9: accept the whole comic-archive family. Non-zip formats (and
    # .cbz files that are secretly RAR/7z/tar) are converted to real
    # ZIP-backed CBZs on the spot by cbz_ensure_zip below.
    if not orig_name.lower().endswith(('.cbz', '.cbr', '.cb7', '.cbt')):
        return jsonify({"error": "Only .cbz / .cbr / .cb7 / .cbt files are accepted"}), 400
    os.makedirs(CBZ_UPLOAD_DIR, exist_ok=True)
    safe_name = re.sub(r'[^\w\s\-.]', '_', os.path.basename(orig_name))
    dest_path = os.path.join(CBZ_UPLOAD_DIR, safe_name)
    if os.path.exists(dest_path):
        base, ext = os.path.splitext(safe_name)
        dest_path = os.path.join(CBZ_UPLOAD_DIR,
                                 f"{base}_{int(time.time() * 1000)}{ext}")
    f.save(dest_path)
    try:
        real_path, note = cbz_ensure_zip(dest_path)
    except ValueError as e:
        try:
            os.remove(dest_path)
        except OSError:
            pass
        return jsonify({"error": str(e)}), 400
    if real_path != dest_path:
        # A converted copy replaced the original upload — drop the original.
        try:
            os.remove(dest_path)
        except OSError:
            pass
    size = os.path.getsize(real_path)
    detected = cbz_detect_chapter_number(orig_name)
    return jsonify({
        "path": real_path,
        "name": orig_name,
        "size": size,
        "detected_chapter": detected,
        "note": note or "",
    })

@app.route("/api/cbz/upload-images", methods=["POST"])
def api_cbz_upload_images():
    files = request.files.getlist('files')
    image_files = [f for f in files
                   if (f.filename or '').rsplit('.', 1)[-1].lower() in IMAGE_EXTS]
    if not image_files:
        return jsonify({"error": "No supported image files found"}), 400
    ts = int(time.time() * 1000)
    img_dir = os.path.join(CBZ_UPLOAD_DIR, f"imgbundle_{ts}")
    os.makedirs(img_dir, exist_ok=True)
    saved = []
    for f in image_files:
        safe_name = re.sub(r'[^\w\s\-.]', '_', os.path.basename(f.filename or f'image_{len(saved)}'))
        dest = os.path.join(img_dir, safe_name)
        f.save(dest)
        saved.append(dest)
    saved.sort(key=lambda p: cbz_sort_key(os.path.basename(p)))
    cbz_name = f"imgbundle_{ts}.cbz"
    cbz_path = os.path.join(CBZ_UPLOAD_DIR, cbz_name)
    with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_STORED) as zf:
        for fp in saved:
            zf.write(fp, os.path.basename(fp))
    shutil.rmtree(img_dir, ignore_errors=True)
    size = os.path.getsize(cbz_path)
    display_name = f"{len(saved)} images (bundled)"
    return jsonify({
        "path": cbz_path,
        "name": display_name,
        "size": size,
        "detected_chapter": "",
    })

@app.route("/api/cbz/upload-zip", methods=["POST"])
def api_cbz_upload_zip():
    """v1.7: accept a generic .zip in the CBZ Processor.

    A .zip is handled one of two ways depending on what's inside:
      • If it contains one or more nested .cbz files, each is extracted
        into MDF/.mdf_uploads/ and returned as its own queue item — a
        "bundle" zip becomes several chapters.
      • Otherwise, if it contains image files (optionally nested in
        sub-folders), the archive itself is kept as a single CBZ source;
        cbz_process_worker reads and renames its pages exactly like it
        does for a real .cbz.

    Response shape is always {"items": [ {path,name,size,detected_chapter}, … ]}.
    """
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files['file']
    orig_name = f.filename or ""
    if not orig_name.lower().endswith('.zip'):
        return jsonify({"error": "Only .zip files are accepted"}), 400
    os.makedirs(CBZ_UPLOAD_DIR, exist_ok=True)

    ts = int(time.time() * 1000)
    tmp_zip = os.path.join(CBZ_UPLOAD_DIR, f"_ziptmp_{ts}.zip")
    f.save(tmp_zip)

    def _drop_tmp():
        try:
            os.remove(tmp_zip)
        except OSError:
            pass

    items = []
    try:
        with zipfile.ZipFile(tmp_zip, 'r') as zf:
            members = [i.filename for i in zf.infolist() if not i.is_dir()]
            cbz_members = [n for n in members if n.lower().endswith(
                ('.cbz', '.cbr', '.cb7', '.cbt'))]
            image_members = [n for n in members
                             if '.' in n
                             and n.rsplit('.', 1)[-1].lower() in IMAGE_EXTS]

            if cbz_members:
                # Bundle zip → split each nested .cbz into its own upload.
                for member in cbz_members:
                    base = os.path.basename(member)
                    safe_name = re.sub(r'[^\w\s\-.]', '_', base)
                    dest_path = os.path.join(CBZ_UPLOAD_DIR, safe_name)
                    if os.path.exists(dest_path):
                        b, ext = os.path.splitext(safe_name)
                        dest_path = os.path.join(
                            CBZ_UPLOAD_DIR,
                            f"{b}_{int(time.time() * 1000)}{ext}")
                    with zf.open(member) as src, open(dest_path, 'wb') as out:
                        shutil.copyfileobj(src, out)
                    # v1.9: nested archives get the same normalization as
                    # direct uploads — a bundled .cbr still ends up a real
                    # ZIP CBZ. A failure is noted but the item stays queued
                    # so the processor can report it per-file.
                    note = ""
                    try:
                        real_path, note_ = cbz_ensure_zip(dest_path)
                        if real_path != dest_path:
                            try:
                                os.remove(dest_path)
                            except OSError:
                                pass
                            dest_path = real_path
                        note = note_ or ""
                    except ValueError as e:
                        note = f"⚠ {e}"
                    items.append({
                        "path": dest_path,
                        "name": base,
                        "size": os.path.getsize(dest_path),
                        "detected_chapter": cbz_detect_chapter_number(base),
                        "note": note,
                    })
            elif not image_members:
                raise ValueError("Zip contains no .cbz or image files")
    except zipfile.BadZipFile:
        _drop_tmp()
        return jsonify({"error": "Not a valid .zip file"}), 400
    except ValueError as e:
        _drop_tmp()
        return jsonify({"error": str(e)}), 400

    if items:
        # Nested .cbz files were extracted; the wrapper zip is done with.
        _drop_tmp()
        return jsonify({"items": items})

    # Image zip → keep the archive itself as one CBZ source (rename to .cbz
    # so downstream display and cleanup behave like every other queue item).
    safe_cbz = os.path.splitext(re.sub(r'[^\w\s\-.]', '_',
                                       os.path.basename(orig_name)))[0] + '.cbz'
    dest_path = os.path.join(CBZ_UPLOAD_DIR, safe_cbz)
    if os.path.exists(dest_path):
        b, ext = os.path.splitext(safe_cbz)
        dest_path = os.path.join(CBZ_UPLOAD_DIR, f"{b}_{ts}{ext}")
    shutil.move(tmp_zip, dest_path)
    return jsonify({"items": [{
        "path": dest_path,
        "name": orig_name,
        "size": os.path.getsize(dest_path),
        "detected_chapter": cbz_detect_chapter_number(orig_name),
    }]})

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
                yield "data: " + json.dumps(msg) + "\n\n"
                if msg["type"] == "all_done":
                    break
            except queue.Empty:
                yield 'data: {"type": "ping"}\n\n'
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
    # Ensure the default base + Downloaded/ + exported/ subdirs exist
    # so the very first run has a clean folder layout to start from.
    resolve_io_dirs(DOWNLOAD_BASE)
    print("\n  MangaFactory v1.9")
    print(f"  Base folder: {DOWNLOAD_BASE}")
    print(f"    ├─ {DOWNLOAD_SUBDIR}/   (raw downloads)")
    print(f"    ├─ {EXPORT_SUBDIR}/   (packaged CBZs)")
    print(f"    └─ MDF/         (working state — uploads cleared after each export)")
    print(f"  Opening http://localhost:{PORT} in your browser...")
    print("  Press Ctrl+C to quit\n")

    def _open_browser():
        time.sleep(1.2)
        webbrowser.open("http://localhost:" + str(PORT))

    threading.Thread(target=_open_browser, daemon=True).start()

    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False,
            threaded=True)
