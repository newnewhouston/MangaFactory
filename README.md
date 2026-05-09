# MangaFactory v1.6

A single-file Python app with a browser UI for downloading manga and packaging it into CBZ volumes. No pip install, no virtual environment — just run the script.

```bash
python "MangaFactory 1.6.py"
```

Opens `http://localhost:5000` automatically. Press `Ctrl+C` to quit.

---

## What's new in v1.6

- **Working state moved into a dedicated `MDF/` folder.** The hidden `.mdf_libs/` (auto-installed dependencies) and `.mdf_uploads/` (CBZ Processor scratch space) no longer sit next to the script. They now live in `~/Desktop/MangaFactory/MDF/`, so moving or copying the `.py` file no longer abandons or duplicates the deps cache, and the script directory stays clean.
- **CBZ Processor uploads are auto-cleaned after each export.** Once a CBZ Processor job finishes writing to `exported/` (or, in folder-tree mode, to its volume folder), the contents of `.mdf_uploads/` are emptied automatically. No more leftover source files accumulating between jobs.

Everything else from v1.5 — sources, download flow, CBZ packaging, the processor tab — works exactly the same.

---

## Output layout

```
~/Desktop/MangaFactory/        ← base folder (set in the UI)
├── Downloaded/                ← raw page images during download
├── exported/                  ← packaged .cbz volumes
└── MDF/                       ← MangaFactory's own working state (new in v1.6)
    ├── .mdf_libs/             ← auto-installed Python dependencies
    └── .mdf_uploads/          ← CBZ Processor scratch (auto-emptied after export)
```

All four subfolders are created automatically. When CBZ packaging is enabled in Tab 1, raw images in `Downloaded/` are deleted after each volume is successfully packaged into `exported/`. The CBZ Processor's single-CBZ output lands in `exported/` under whatever base folder you point it at; folder-tree mode writes into the base folder directly since it produces a directory of images, not a CBZ.

---

## Sources

MangaFactory auto-detects the source from the URL you paste. Two sources are supported:

**MangaDex** — paste a series URL (`https://mangadex.org/title/…`) or bare UUID. Chapters are fetched via the MangaDex API, deduplicated across scanlation groups, and grouped by volume. Gap detection warns you when chapter numbers are non-consecutive.

**WeebCentral** — paste a series URL (`https://weebcentral.com/series/…`). The full chapter list is scraped directly from the site. Works regardless of how the series labels its chapters — "Chapter 5", "Mission 133", "Episode 12" all parse correctly. Uses `cloudscraper` to handle Cloudflare protection transparently.

---

## Tab 1 — Download

1. Paste a MangaDex or WeebCentral URL and click **Fetch**
2. The chapter list loads, grouped by volume where applicable. A source badge (· MangaDex / · WeebCentral) confirms what was detected
3. Select individual chapters or click a volume header to select the whole volume. Use the filter box to search by chapter number or title
4. Set an output folder (default: `~/Desktop/MangaFactory`)
5. Optionally toggle **Package into CBZ volumes**
6. Click **Download Selected**

Progress streams live — page-by-page and chapter-by-chapter bars, plus a scrolling log. Already-downloaded pages are skipped automatically if you re-run.

### CBZ packaging

When enabled, downloaded images are zipped into `.cbz` files after the download completes. For MangaDex, chapters are grouped by their assigned volume — one `.cbz` per volume. For WeebCentral, which has no volume metadata, all selected chapters go into a single `.cbz`. Raw page images are deleted from `Downloaded/` after each `.cbz` is successfully written into `exported/`.

Output with CBZ packaging (MangaDex):
```
exported/
  series_slug_vol01.cbz
  series_slug_vol02.cbz
  series_slug_vol_unnumbered.cbz
```

Output with CBZ packaging (WeebCentral):
```
exported/
  series_slug_vol_unnumbered.cbz
```

Output without CBZ packaging (either source):
```
Downloaded/
  series_slug_ch01_001.jpg
  series_slug_ch01_002.jpg
  ...
```

---

## Tab 2 — CBZ Processor

Takes existing `.cbz` files (or loose image files) and repackages them with consistent naming, an optional cover image, and your choice of output format.

**Loading files** — three ways, freely mixed:
- Drag `.cbz` or image files onto the drop zone
- Click the drop zone to browse
- After a download, click **Send to CBZ Processor →** to hand off the output folder automatically

**Image files** — raw jpg/png/webp/gif/bmp files are bundled into a temporary `.cbz` automatically (sorted naturally) and added to the queue like any other file.

**Chapter numbers** — auto-detected from filenames via keyword patterns (`chapter`, `ch`, `c`, `#`). Image bundles require a chapter number to be entered manually. Use **Auto-fill** to number a sequence from a starting value.

**Output**:
- **Single CBZ** — all chapters packed into `Volume_XX.cbz`, pages renamed to `Chapter_XX_page_YYY.ext`, cover inserted as `000_cover.ext`
- **Folder Tree** — same naming, written to a `Volume_XX/` directory instead

**After export** — the contents of `MDF/.mdf_uploads/` are wiped automatically (new in v1.6). The directory itself is kept so the next job can write into it without recreating it. Cleanup is best-effort; if a file happens to be locked by another process, it's skipped silently rather than failing the export.

---

## Requirements

- Python 3.8+
- Internet on first run — `flask`, `requests`, and `cloudscraper` are installed automatically into `~/Desktop/MangaFactory/MDF/.mdf_libs/`

---

## Notes

- MangaDex fetches English translations only
- `MDF/.mdf_libs/` holds auto-installed dependencies; `MDF/.mdf_uploads/` holds temporary upload files (auto-cleared after each successful export). Both can be deleted safely between sessions
- Dependencies do not touch your system Python installation
- Upgrading from v1.5: the old `.mdf_libs/` and `.mdf_uploads/` folders next to the script can be deleted — v1.6 will rebuild them in `~/Desktop/MangaFactory/MDF/` on first run

---

## License

MIT
