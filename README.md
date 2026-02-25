# HTML Unembed Tool (Base64 Data-URI Extractor)

Extracts embedded `data:*;base64,...` assets from a large HTML dump (and any inline CSS/JS inside it), writes them into a folder, and rewrites the HTML to reference the extracted files.

Designed for **very large HTML files** (e.g., 120MB+) without loading the whole file into memory.

## What it does

- Finds **base64 `data:` URIs** (e.g. `data:image/png;base64,...`, fonts, etc.)
- Decodes them into real files (e.g. `assets/sha256.png`)
- Rewrites the HTML to use relative links (e.g. `assets/sha256.png`)
- Works in a **streaming** way: safe for huge inputs
- Optional: produces an output `.zip`

## What it does NOT do

- It does **not** download external scripts/libs (e.g. jQuery CDN, Google Maps, Hotjar, etc.)
- If the dump references external services, you may see browser console errors:
  - `jQuery is not defined` → the original page expected jQuery but it’s not embedded
  - `401` / `NoApiKeys` → external API requires auth / keys
  - `file:// origin null` errors → open via a local web server instead of `file://`

## Quickstart

### 1) Install (stdlib-only)
This tool uses only the Python standard library.

```bash
python3 -V
```

### 2) Run
```bash
python3 -m html_unembed_tool --input input.html --out-dir out --assets-dir assets --zip
```

Outputs:
- `out/index.html`
- `out/assets/` (or your chosen assets dir)
- `out.zip` (if `--zip` is set)

### 3) Open it correctly (IMPORTANT)
Do **not** open the output via `file://...` if you want fewer JS issues.

Run a local server:

```bash
cd out
python3 -m http.server 8000
```

Open:
- `http://localhost:8000/index.html`

## CLI options

Typical usage:

```bash
python3 -m html_unembed_tool \
  --input input.html \
  --out-dir out \
  --out-html index.html \
  --assets-dir assets \
  --zip
```

- `--input`     Path to input HTML file
- `--out-dir`   Output directory
- `--out-html`  Output HTML filename (default: `index.html`)
- `--assets-dir` Assets directory name inside `out-dir` (default: `assets`)
- `--zip`       Also create `out.zip`

## Troubleshooting

### `Unexpected token` / broken JS
This usually happens if base64 payloads were chopped (common when base64 spans multiple lines).  
Use the fixed extractor version included in this repo (it allows newlines inside base64 and strips whitespace before decode).

### `jQuery is not defined` / Bootstrap errors
The dump’s scripts expect jQuery, but it isn’t present locally.

Two options:
1) Add a local `jquery.min.js` file and include it before Bootstrap.
2) Use a CDN `<script>` tag (only if you want external dependency).

### External APIs failing
Errors like:
- Google Maps: `NoApiKeys`
- Hotjar: `_hjSettings is not defined`
- Vendor endpoints: `401`

These are expected unless you have keys/tokens and are running on the right domain.