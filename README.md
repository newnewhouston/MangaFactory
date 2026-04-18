# MangaFactory v1.2

**Download · Process · Package**

MangaFactory is a self-contained, single-file Python app that puts a clean browser UI on top of two manga workflow tools: a **MangaDex downloader** and a **CBZ file processor**. Run it with one command — no `pip install` needed.

---

## Features

### Tab 1 — Download (MangaDex)

- Paste any MangaDex series URL or UUID to fetch the full chapter list
- Chapters are grouped by volume with page counts shown at a glance
- **Gap detection** — warns you when chapter numbers have holes (e.g. Ch. 12 → Ch. 15), so you know what's not yet translated
- **Deduplication** — only one entry per chapter number, no duplicates from multiple scanlation groups
- Filter chapters by keyword; select/deselect by individual chapter or entire volume at once
- Optional **CBZ packaging** — after download, chapters are automatically grouped by MangaDex volume and packaged into one `.cbz` file per volume. Once a volume's `.cbz` is written, the raw page images that went into it are removed, so the output folder only contains the packaged volumes. With CBZ packaging off, the raw images are left in place.
- Real-time progress: live page-by-page and chapter-by-chapter progress bars with a streaming log
- Already-downloaded pages are skipped automatically on re-runs
- Output defaults to `~/Downloads/manga`; fully configurable

### Tab 2 — CBZ Processor

- Point it at any folder of `.cbz` files and click **Scan**
- Chapter numbers are **auto-detected** from filenames using keyword patterns (`chapter`, `ch`, `c`, `#`) with a fallback to numeric tokens
- Pages inside each CBZ are renamed to a consistent scheme: `Chapter_XX_page_YYY.ext`
- A cover image can be injected as `000_cover.{ext}` — always the first file in the archive
- Two output modes:
  - **Single CBZ** — everything packed into one `Volume_XX.cbz`
  - **Folder Tree** — pages extracted into a `Volume_XX/` directory (no re-zipping)
- **Auto-fill** button: set the first chapter number and fill the rest sequentially
- Real-time progress bars and a per-file status log

### Workflow shortcut

After a download completes, a **"Send to CBZ Processor →"** button pre-fills the processor tab with the output folder and auto-scans it, so you can move straight from downloading to packaging without copying any paths.

---

## Requirements

- Python 3.8 or later
- Internet connection on first run (to bootstrap `flask` and `requests` into a local `.mdf_libs/` folder)

No virtual environment, no manual `pip install`.

---

## Usage

```bash
python MangaFactory.py
```

The app installs its own dependencies on first run, then opens `http://localhost:5000` in your browser automatically.

Press `Ctrl+C` in the terminal to quit.

---

## Download Tab — Quick Start

1. Paste a MangaDex URL or series UUID into the input and click **Fetch**
2. Review the chapter list; check any gap warnings
3. Select chapters (or click a volume header to select the whole volume)
4. Choose an output folder (default: `~/Downloads/manga`)
5. Toggle **Package into CBZ volumes** if you want `.cbz` files per volume
6. Click **Download Selected** and watch the live progress

---

## CBZ Processor Tab — Quick Start

1. Enter the path to a folder containing `.cbz` files and click **Scan**
2. Verify or correct the auto-detected chapter numbers
3. Optionally set a volume number and a cover image path
4. Choose **Single CBZ** or **Folder Tree** output mode
5. Set an output folder and click **Process Files**

---

## Output File Structure

**Downloaded images (no CBZ):**
```
~/Downloads/manga/
  series_slug/
    series_slug_ch01_001.jpg
    series_slug_ch01_002.jpg
    ...
```

**With CBZ packaging enabled:**
```
~/Downloads/manga/
  series_slug/
    series_slug_vol01.cbz
    series_slug_vol02.cbz
    series_slug_vol_unnumbered.cbz   ← chapters with no volume assigned
```

Raw page images are deleted automatically after each volume's `.cbz` is built, so only the packaged volumes remain. If CBZ packaging is turned off, nothing is deleted — the raw images are left exactly where they were downloaded.

**CBZ Processor output (Single CBZ mode):**
```
output/
  Volume_03.cbz
    000_cover.jpg
    Chapter_15_page_01.jpg
    Chapter_15_page_02.jpg
    Chapter_16_page_01.jpg
    ...
```

**CBZ Processor output (Folder Tree mode):**
```
output/
  Volume_03/
    000_cover.jpg
    Chapter_15_page_01.jpg
    Chapter_16_page_01.jpg
    ...
```

---

## How It Works

MangaFactory is a local Flask web server bundled with its own single-page HTML/JS UI (served inline — no build step, no separate files). The backend streams real-time progress to the browser using [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events).

Downloads use the [MangaDex API](https://api.mangadex.org) with respectful rate-limiting between page requests.

---

## Notes

- Only English-translated chapters are fetched from MangaDex
- If a chapter has no volume assigned on MangaDex, it is placed in a `vol_unnumbered.cbz` when CBZ packaging is enabled
- When CBZ packaging is enabled, raw page images are removed after each volume's `.cbz` is successfully written; if the CBZ step fails for a volume, that volume's raw files are kept so you can retry without re-downloading
- The CBZ Processor uses natural sort order for filenames, so `ch9` correctly comes before `ch10`
- Dependencies are installed into `.mdf_libs/` next to the script and do not touch your system Python

---

## License

MIT
