# MangaFactory v1.7

A single-file Python app with a browser UI for downloading manga and packaging it into CBZ files. No pip install, no virtual environment — just run the script.

```bash
python "MangaFactory 1.7.py"
```

Opens `http://localhost:5000` automatically. Press `Ctrl+C` to quit.

---

## What's new in v1.7

- **Download: per-chapter CBZ packaging.** The Download tab's CBZ toggle is now a three-way **Packaging** selector — **Images** (raw pages, no packaging), **One CBZ / Volume** (the previous grouping behavior), or **One CBZ / Chapter** (new — each selected chapter becomes its own standalone `.cbz`).
- **CBZ Processor: `.zip` upload.** Alongside `.cbz` files and loose images, you can now drop or browse a `.zip` archive. A zip of images is treated as a single chapter's pages; a zip that bundles several `.cbz` files is split so each nested chapter is queued on its own.
- **CBZ Processor: chapter-based output naming.** When the **Volume Number** field is left blank but the queued files carry chapter numbers, the output is named after the chapter instead of the generic `New Volume` — a single chapter → `7.cbz`, several chapters → a `7-9` range.
- **comix.to support (browser grab).** comix.to encrypts its API, so it can't be scraped server-side. A new panel in the Download tab provides a bookmarklet that grabs an open chapter straight from your logged-in browser and saves it as a `.cbz` for the CBZ Processor.

Everything else from v1.6 — sources, the download flow, the processor tab, and the `MDF/` working-state layout — works the same.

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

All subfolders are created automatically. When CBZ packaging is enabled in Tab 1, raw images in `Downloaded/` are deleted after each `.cbz` is successfully written into `exported/`. The CBZ Processor's single-CBZ output lands in `exported/`; folder-tree mode writes into the base folder directly since it produces a directory of images, not a CBZ.

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
| **🖼 Images** | Raw page images only, left in `Downloaded/`. No `.cbz` is built. |
| **📚 One CBZ / Volume** | Chapters grouped by volume → one `.cbz` per volume in `exported/`. Sources without volume info (WeebCentral, comix) produce a single combined `.cbz`. |
| **📦 One CBZ / Chapter** | *(new)* Each selected chapter packaged into its own standalone `.cbz` in `exported/`. |

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

1. In MangaFactory, drag the **📥 Comix → CBZ** button to your browser's bookmarks bar (one-time setup). If you can't drag it, use **Copy bookmarklet** and paste it as a new bookmark's URL, or **Copy console snippet** to paste into the page's DevTools console.
2. Open any comix.to chapter in your browser.
3. Click the bookmark. A small overlay reads the page-image pattern and downloads each page one at a time (gently paced to avoid the site's rate limiting), then saves a `{Series} - Ch {n}.cbz`.
4. Drop that file into **Tab 2 — CBZ Processor** to rename the pages and finish the volume.

The grab runs entirely in your browser and builds the CBZ client-side — no external libraries, no server round-trip. Pages are saved as `.webp` (CBZ handles them fine). Very long chapters take proportionally longer since pages are fetched one at a time.

---

## Tab 2 — CBZ Processor

Takes existing `.cbz` files, a `.zip` archive, or loose image files and repackages them with consistent naming, an optional cover image, and your choice of output format.

**Loading files** — freely mixed:
- Drag `.cbz`, `.zip`, or image files onto the drop zone
- Click the drop zone to browse
- After a download, click **Send to CBZ Processor →** to hand off the output folder automatically

**`.zip` archives** *(new in v1.7)* — a dropped or uploaded `.zip` is inspected: if it contains image files (even nested in sub-folders) it's treated as one chapter's pages; if it bundles several `.cbz` files, each nested chapter is split out and queued separately.

**Image files** — raw jpg/png/webp/gif/bmp files are bundled into a temporary `.cbz` automatically (sorted naturally) and added to the queue like any other file.

**Chapter numbers** — auto-detected from filenames via keyword patterns (`chapter`, `ch`, `c`, `#`). Image bundles require a chapter number to be entered manually. Use **Auto-fill** to number a sequence from a starting value.

**Volume & output name** — the output is normally named `Volume_XX` from the **Volume Number** field. *(New in v1.7)* if you leave Volume Number blank but the queued files have chapter numbers, the output is named after the chapter instead: a single chapter → `7.cbz`, several chapters → a `7-9` range. The "Folder/CBZ named:" preview updates live as you edit chapters.

**Output**:
- **Single CBZ** — all chapters packed into one file, pages renamed to `Chapter_XX_page_YYY.ext`, cover inserted as `000_cover.ext`
- **Folder Tree** — same naming, written to a directory instead

**After export** — the contents of `MDF/.mdf_uploads/` are wiped automatically. The directory itself is kept so the next job can write into it. Cleanup is best-effort; a locked file is skipped silently rather than failing the export.

---

## Requirements

- Python 3.8+
- Internet on first run — `flask`, `requests`, and `cloudscraper` are installed automatically into `~/Desktop/MangaFactory/MDF/.mdf_libs/`

---

## Notes

- MangaDex fetches English translations only
- The comix.to grab runs in your browser, not the Python app — it needs no extra dependencies and works with whatever you can already read while logged in
- `MDF/.mdf_libs/` holds auto-installed dependencies; `MDF/.mdf_uploads/` holds temporary upload files (auto-cleared after each successful export). Both can be deleted safely between sessions
- Dependencies do not touch your system Python installation

---

## License

MIT
