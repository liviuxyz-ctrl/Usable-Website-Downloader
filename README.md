# HTML Unembed Tool (Base64 Data-URI Extractor + Optional External Asset Downloader)

Turns a giant exported HTML dump (often 100MB+) into a usable folder:
- Extracts embedded `data:*;base64,...` assets into files
- Rewrites the HTML so links point to those files
- **Optional:** downloads external `http(s)` assets referenced by `src=`, `href=`, or `url(...)` into local files and rewrites links (offline-friendly)
- Extracts inline `<style>` blocks into real CSS files by default

This is designed to work **streaming** (no “load the whole HTML into RAM”).

## What this repo is for

This repo contains the Python CLI app that turns one giant HTML export into a
more normal project folder:

- `index.html`
- `assets/` for extracted base64 images, fonts, SVGs, JSON blobs, etc.
- `styles/` for extracted inline CSS
- `externals/` for downloaded external scripts, stylesheets, fonts, and images when `--fetch-externals` is used

The source code to track in Git is the app itself:

- `html_unembed_tool/`
- `README.md`
- `pyproject.toml`
- `requirements.txt`
- `LICENSE`

Generated folders and large inputs such as `out/`, `projects/*/out/`, `*.zip`,
and raw 50MB+ HTML dumps should normally stay out of Git unless you explicitly
want to version those artifacts.

## jQuery / JavaScript support

The tool supports jQuery in the static-asset sense:

- If jQuery is embedded as a base64 `data:` URI, it is extracted into `assets/`.
- If jQuery is referenced by a static `<script src="...">` URL, `--fetch-externals` can download it into `externals/` and rewrite the script tag.
- If inline JavaScript contains base64 `data:` URIs, those data URIs can be extracted and rewritten.

The tool does **not** convert jQuery code into a modern app repo by itself. It
does not split inline scripts into modules, infer components, rewrite jQuery to
React/Vue/etc., or fix runtime API calls. It preserves and relocates static
files so the exported page becomes easier to inspect, serve, and refactor.

## Install

No dependencies (stdlib-only). Python 3.10+ recommended.

From a checkout:

```bash
python3 -m pip install -e .
```

## Usage

### 1) Extract embedded base64 assets
```bash
python3 -m html_unembed_tool --input input.html --out-dir out --assets-dir assets
```

### 2) Also download external assets (JS/CSS/images/fonts) into local files
If your HTML contains relative URLs, provide a base URL to resolve them.

```bash
python3 -m html_unembed_tool \
  --input input.html \
  --out-dir out \
  --assets-dir assets \
  --fetch-externals \
  --base-url https://example.com/ \
  --externals-dir externals
```

### 3) Open the output correctly
Avoid opening via `file://` (it breaks cookies/origin and some scripts).

```bash
cd out
python3 -m http.server 8000
```

Open:
- http://localhost:8000/index.html

Or let the tool start the server after extraction:

```bash
python3 -m html_unembed_tool \
  --input input.html \
  --out-dir out \
  --serve-after
```

Or choose the server port directly:

```bash
python3 -m html_unembed_tool \
  --input input.html \
  --out-dir out \
  --run-on-web 8000
```

## CLI Options

- `--input` (required): input HTML file
- `--out-dir` (required): output directory
- `--out-html`: output HTML filename (default: `index.html`)
- `--assets-dir`: extracted embedded assets folder (default: `assets`)
- `--fetch-externals`: download external `http(s)` assets and rewrite links
- `--base-url`: used to resolve relative URLs (recommended if you see relative `src`/`href`)
- `--externals-dir`: folder for downloaded externals (default: `externals`)
- `--timeout-s`: per-request timeout (default: 20)
- `--max-download-mb`: max size per external asset (default: 50)
- `--zip`: also zip the `out-dir` into `out-dir.zip`
- `--serve-after`: start a local static web server for `out-dir` after extraction finishes
- `--serve-host`: host for `--serve-after` / `--run-on-web` (default: `127.0.0.1`)
- `--serve-port`: port for `--serve-after` (default: `8000`; use `0` for any free port)
- `--run-on-web PORT`: alias for `--serve-after --serve-port PORT`

## Development

Run the test suite with the Python standard library:

```bash
python3 -m unittest discover -s tests
```

The repository should track the extractor source and docs only. Keep giant raw
HTML files, generated `out/` folders, generated project folders, and archives
out of Git.

## Notes / limitations (important)

- This **cannot** magically fix runtime API calls (e.g. `401` from some backend). It only makes static assets local.
- Some third-party assets may be protected by license/terms — only download what you’re allowed to mirror.
- JS that dynamically loads more scripts may still attempt network calls. This tool only catches static `src/href/url()` references in the HTML (plus `url()` inside downloaded CSS).

## License

MIT (see LICENSE).


## Inline <style> extraction (default: ON)

The tool will extract every inline `<style> ... </style>` block into separate CSS files under:

- `out/styles/inline-style-001.css`, `out/styles/inline-style-002.css`, ...

and it replaces the original `<style>` blocks with:

- `<link rel="stylesheet" href="styles/inline-style-001.css">`

Because these CSS files live in a subfolder, the tool automatically rewrites local `url(...)` references
that point to files that already exist under `out/` (like `assets/...`) to correct relative paths
(e.g. `../assets/...`).

Disable it with:

```bash
python3 -m html_unembed_tool ... --no-extract-style-tags
```
