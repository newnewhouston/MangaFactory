# MangaFactory v1.9

A single-file Python app with a browser UI for downloading manga and packaging it into CBZ files. No pip install, no virtual environment — just run the script.

```bash
python "MangaFactory 1.9.py"
```

Opens `http://localhost:5000` automatically. Press `Ctrl+C` to quit.

---

## What's new in v1.9

**Drop-anything CBZ Processor.** Many `.cbz` files in the wild aren't actually ZIP archives — they're RAR (`.cbr`), 7-Zip (`.cb7`) or tar (`.cbt`) archives that were renamed. Earlier versions rejected these with a bare *"File is not a zip file"*. Now:

- **Magic-byte sniffing.** Every queued file is identified by its real content, not its extension — at upload time *and* again at process time (so folder-scanned files are covered too).
- **Automatic conversion.** A disguised RAR/7z/tar archive is unpacked with the first available extractor and its pages repacked into a genuine ZIP-backed `.cbz`. Extractors are tried in order: WinRAR's `UnRAR`, `7z` (7-Zip), Windows' bundled `tar.exe` (bsdtar, ships with Windows 10+), and Python's own `tarfile`. No new Python dependencies.
- **`.cbr` / `.cb7` / `.cbt` accepted directly** — in the drop zone, the file picker, folder scans, and nested inside bundle `.zip` files. Chapter-number detection understands the new extensions.
- **Clear errors instead of the zipfile mumble.** A file that can't be fixed now says what it actually is: an empty file (failed download), a PDF, an HTML error page saved by the site, a corrupted/truncated ZIP, or an archive with no extractor available (with the hint to install 7-Zip or WinRAR).
- **Conversion is visible.** The upload status and each file's row note what was converted and how many pages came out; the processing log repeats it. Failed uploads no longer vanish after 3 seconds — the error stays on screen.
- **Progress rows match your filenames.** The worker now reports files by the name you dropped, so status dots and errors line up even for converted or renamed uploads.

Everything else — both download sources, the comix.to browser grab, packaging modes, and the `MDF/` layout — works exactly as in v1.8.

---

## Output layout

```
~/Desktop/MangaFactory/        ← base folder (set in the UI)
├── Downloaded/                ← raw page images during download
├── exported/                  ← packaged .cbz files
└── MDF/                       ← MangaFactory's own working state
    ├── .mdf_libs/             ← auto-installed Python dependencies
    └── .mdf_uploads/          ← CBZ Processor scratch (auto-emptied after export)
```

All subfolders are created automatically. When CBZ packaging is enabled in Tab 1, raw images in `Downloaded/` are deleted after each `.cbz` is successfully written into `exported/`. The CBZ Processor's single-CBZ output lands in `exported/`; folder-tree mode writes into the base folder directly since it produces a directory of images, not a CBZ. Converted archives live in `.mdf_uploads/` and are wiped with the rest of the scratch space after each export.

---

## Sources

MangaFactory auto-detects MangaDex and WeebCentral from the URL you paste. comix.to is handled separately through a browser grab (see below).

**MangaDex** — paste a series URL (`https://mangadex.org/title/…`) or bare UUID. Chapters are fetched via the MangaDex API, deduplicated across scanlation groups, and grouped by volume. Gap detection warns you when chapter numbers are non-consecutive.

**WeebCentral** — paste a series URL (`https://weebcentral.com/series/…`). The full chapter list is scraped directly from the site. Works regardless of how the series labels its chapters — "Chapter 5", "Mission 133", "Episode 12" all parse correctly. Uses `cloudscraper` to handle Cloudflare protection transparently.

**comix.to** — handled via a one-click browser grab rather than a pasted URL, because comix.to signs its API requests and encrypts the responses. See [comix.to · Browser Grab](#comixto--browser-grab).

---

## Tab 1 — Download

1. Paste a MangaDex or WeebCentral URL and click **Fetch**
2. The chapter list loads, grouped by volume where applicable. A source badge (· MangaDex / · WeebCentral) confirms what was detected
3. Select individual chapters or click a volume header to select the whole volume. Use the filter box to search by chapter number or title
4. Set an output folder (default: `~/Desktop/MangaFactory`)
5. Choose a **Packaging** mode (see below)
6. Click **Download Selected**

Progress streams live — page-by-page and chapter-by-chapter bars, plus a scrolling log. Already-downloaded pages are skipped automatically if you re-run.

### Packaging modes

| Mode | Result |
|---|---|
| **Images** | Raw page images only, left in `Downloaded/`. No `.cbz` is built. |
| **One CBZ / Volume** | Chapters grouped by volume → one `.cbz` per volume in `exported/`. Sources without volume info (WeebCentral, comix) produce a single combined `.cbz`. |
| **One CBZ / Chapter** | Each selected chapter packaged into its own standalone `.cbz` in `exported/`. |

In both CBZ modes, raw page images are deleted from `Downloaded/` after each `.cbz` is successfully written.

Output — One CBZ / Volume (MangaDex):
```
exported/
  series_slug_vol01.cbz
  series_slug_vol02.cbz
  series_slug_vol_unnumbered.cbz
```

Output — One CBZ / Chapter:
```
exported/
  series_slug_ch01.cbz
  series_slug_ch02.cbz
  series_slug_ch12_5.cbz
```

Output — Images:
```
Downloaded/
  series_slug_ch01_001.jpg
  series_slug_ch01_002.jpg
  ...
```

### comix.to · Browser Grab

comix.to encrypts its API, so MangaFactory can't fetch it server-side. Instead, the **Comix.to · Browser Grab** panel in the Download tab provides a bookmarklet that pulls a chapter straight from your own logged-in browser, where the pages are already decrypted.

1. In MangaFactory, drag the **Comix → CBZ** button to your browser's bookmarks bar (one-time setup). If you can't drag it, use **Copy bookmarklet** and paste it as a new bookmark's URL, or **Copy console snippet** to paste into the page's DevTools console.
2. Open any comix.to chapter in your browser.
3. Click the bookmark. A small overlay reads the page-image pattern and downloads each page one at a time (gently paced to avoid the site's rate limiting), then saves a `{Series} - Ch {n}.cbz`.
4. Drop that file into **Tab 2 — CBZ Processor** to rename the pages and finish the volume.

The grab runs entirely in your browser and builds the CBZ client-side — no external libraries, no server round-trip. Pages are saved as `.webp` (CBZ handles them fine). Very long chapters take proportionally longer since pages are fetched one at a time.

---

## Tab 2 — CBZ Processor

Takes existing comic archives (`.cbz`, `.cbr`, `.cb7`, `.cbt`), a `.zip` archive, or loose image files and repackages them with consistent naming, an optional cover image, and your choice of output format.

**Loading files** — freely mixed:
- Drag comic archives, `.zip`, or image files onto the drop zone
- Click the drop zone to browse
- After a download, click **Send to CBZ Processor →** to hand off the output folder automatically

**Non-zip archives (new in v1.9)** — every file is sniffed by content. RAR/7z/tar archives — including ones misleadingly named `.cbz` — are converted to real ZIP-backed CBZs automatically using whatever extractor is installed (WinRAR, 7-Zip, or Windows' bundled `tar.exe`). Files that can't be processed get a plain-language explanation of what they actually are.

**`.zip` archives** — a dropped or uploaded `.zip` is inspected: if it contains image files (even nested in sub-folders) it's treated as one chapter's pages; if it bundles several comic archives, each nested chapter is split out (and converted if needed) and queued separately.

**Image files** — raw jpg/png/webp/gif/bmp files are bundled into a temporary `.cbz` automatically (sorted naturally) and added to the queue like any other file.

**Chapter numbers** — auto-detected from filenames via keyword patterns (`chapter`, `ch`, `c`, `#`). Image bundles require a chapter number to be entered manually. Use **Auto-fill** to number a sequence from a starting value.

**Volume & output name** — the output is normally named `Volume_XX` from the **Volume Number** field. If you leave Volume Number blank but the queued files have chapter numbers, the output is named after the chapter instead: a single chapter → `7.cbz`, several chapters → a `7-9` range. The "Folder/CBZ named:" preview updates live as you edit chapters.

**Output**:
- **Single CBZ** — all chapters packed into one file, pages renamed to `Chapter_XX_page_YYY.ext`, cover inserted as `000_cover.ext`
- **Folder Tree** — same naming, written to a directory instead

**After export** — the contents of `MDF/.mdf_uploads/` are wiped automatically (including any converted archives). The directory itself is kept so the next job can write into it. Cleanup is best-effort; a locked file is skipped silently rather than failing the export.

---

## Requirements

- Python 3.8+
- Internet on first run — `flask`, `requests`, and `cloudscraper` are installed automatically into `~/Desktop/MangaFactory/MDF/.mdf_libs/`
- For RAR/7z conversion: any of WinRAR, 7-Zip, or Windows' bundled `tar.exe` (present by default on Windows 10+). Nothing extra is needed for tar/gzip — Python handles those natively

---

## Notes

- MangaDex fetches English translations only
- The comix.to grab runs in your browser, not the Python app — it needs no extra dependencies and works with whatever you can already read while logged in
- `MDF/.mdf_libs/` holds auto-installed dependencies; `MDF/.mdf_uploads/` holds temporary upload files (auto-cleared after each successful export). Both can be deleted safely between sessions
- Dependencies do not touch your system Python installation

---

## License

MIT
