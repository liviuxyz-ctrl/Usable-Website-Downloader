#!/usr/bin/env python3
"""Extract base64-embedded data: URIs from a large HTML (or any text) file.

Why this exists:
- Some "one-click import" tools export a single giant HTML where images/fonts/etc are embedded as
  data URIs (data:<mime>;base64,<payload>). That can be 100+ MB and painful to work with.

What this script does:
- Streams the input file (does NOT load it fully into RAM).
- Finds base64 data URIs (case-insensitive for 'data:' and ';base64,').
- Writes each decoded asset to an assets folder.
- Rewrites the file so the original data URI is replaced with a relative file URL.
- Optionally zips the output folder.

It also works for data URIs inside inline CSS (url(data:...)), inline JS strings, etc.

Optional feature:
- Can also download external http(s) assets referenced via src/href/url(...) into local files
  and rewrite links, so the output can run offline.

Example:
  python3 extract_embedded_assets.py \
    --input Travel_Advantage_Elite_clean.html \
    --out-dir out \
    --assets-dir assets \
    --zip

Notes:
- Only base64 data URIs are extracted. Non-base64 data URIs are left untouched.
- Filenames are based on SHA-256 of decoded bytes (deduped automatically).
"""

from __future__ import annotations

import argparse
import binascii
import functools
import hashlib
import http.server
import mimetypes
import os
import shutil
import socketserver
import sys
import time
import uuid
import zipfile
import re
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple, List


BASE64_MARKER = b";base64,"
DATA_PREFIX = b"data:"

# End-of-data-uri delimiters.
# IMPORTANT: Do NOT treat whitespace/newlines as delimiters.
# Some exporters wrap long base64 payloads across lines; whitespace must be allowed inside
# the base64 payload (we strip it during decode).
DELIMS: Tuple[int, ...] = tuple(set(b"\"')>"))



@dataclass
class ExtractStats:
    assets_found: int = 0
    assets_written: int = 0
    bytes_read: int = 0
    bytes_decoded: int = 0
    replaced_spans: int = 0


def _guess_extension(mime: str) -> str:
    """Best-effort extension for a mime type."""
    mime = (mime or "").strip().lower()

    # Common overrides.
    overrides = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
        "image/x-icon": ".ico",
        "font/woff": ".woff",
        "font/woff2": ".woff2",
        "application/font-woff": ".woff",
        "application/font-woff2": ".woff2",
        "application/octet-stream": ".bin",
        "text/plain": ".txt",
        "text/css": ".css",
        "text/javascript": ".js",
        "application/javascript": ".js",
        "application/json": ".json",
    }
    if mime in overrides:
        return overrides[mime]

    ext = mimetypes.guess_extension(mime) or ""
    # mimetypes may return .jpe; normalize.
    if ext == ".jpe":
        return ".jpg"
    return ext or ".bin"


def _iter_first_delim(buf: bytes, start: int) -> int:
    """Return index of the first delimiter after start, or -1."""
    # Manual scan is faster than multiple .find() calls for many delimiters.
    mv = memoryview(buf)
    for idx in range(start, len(buf)):
        if mv[idx] in DELIMS:
            return idx
    return -1


def _decode_base64_streaming(
    parts: Iterable[memoryview | bytes],
    out_fp,
    sha256,
) -> int:
    """Decode base64 parts incrementally, writing decoded bytes, updating sha256.

    Returns total decoded bytes.
    """
    decoded_total = 0
    rem = b""

    for part in parts:
        if isinstance(part, memoryview):
            # memoryview -> bytes WITHOUT copy is not guaranteed; keep it as bytes-like.
            chunk = part.tobytes()  # safe and simple; parts are modest sized in our chunking.
        else:
            chunk = part

        if not chunk:
            continue

        # Combine with remainder so we decode full quartets.
        chunk = rem + chunk

        # Strip obvious whitespace (just in case an exporter formatted long base64 lines)
        # This keeps the remainder logic sane.
        if b"\n" in chunk or b"\r" in chunk or b"\t" in chunk or b" " in chunk:
            chunk = b"".join(chunk.split())

        if not chunk:
            rem = b""
            continue

        keep = len(chunk) % 4
        if keep:
            rem = chunk[-keep:]
            chunk = chunk[:-keep]
        else:
            rem = b""

        if chunk:
            try:
                decoded = binascii.a2b_base64(chunk)
            except binascii.Error:
                # Fallback: non-strict decoder.
                decoded = binascii.a2b_base64(chunk + b"=\n")
            out_fp.write(decoded)
            sha256.update(decoded)
            decoded_total += len(decoded)

    if rem:
        # Pad remainder to a multiple of 4.
        pad = (-len(rem)) % 4
        rem_padded = rem + (b"=" * pad)
        try:
            decoded = binascii.a2b_base64(rem_padded)
        except binascii.Error:
            decoded = b""
        if decoded:
            out_fp.write(decoded)
            sha256.update(decoded)
            decoded_total += len(decoded)

    return decoded_total


def _safe_rel_url(path: Path) -> str:
    """Convert Path to a forward-slash relative URL string."""
    return path.as_posix()


def extract_data_uris_stream(
    in_path: Path,
    out_path: Path,
    assets_dir: Path,
    assets_url_prefix: str = "assets",
    chunk_size: int = 4 * 1024 * 1024,
    tail_keep: int = 128 * 1024,
    log_every_seconds: float = 2.0,
    max_assets: Optional[int] = None,
) -> Tuple[ExtractStats, Dict[str, str]]:
    """Stream-extract base64 data URIs from in_path into assets_dir and rewrite to out_path.

    Returns (stats, map_sha_to_rel_url).
    """
    stats = ExtractStats()
    assets_dir.mkdir(parents=True, exist_ok=True)

    sha_to_rel: Dict[str, str] = {}

    def write_asset_from_b64(mime_main: str, b64_mv: memoryview) -> str:
        nonlocal sha_to_rel, stats

        ext = _guess_extension(mime_main)

        tmp_name = f"._tmp_{uuid.uuid4().hex}{ext}"
        tmp_path = assets_dir / tmp_name

        sha = hashlib.sha256()
        with tmp_path.open("wb") as fp:
            decoded_len = _decode_base64_streaming([b64_mv], fp, sha)
        stats.bytes_decoded += decoded_len

        digest = sha.hexdigest()
        final_name = f"{digest}{ext}"
        final_path = assets_dir / final_name

        if digest in sha_to_rel and final_path.exists():
            # Duplicate content; discard temp.
            try:
                tmp_path.unlink(missing_ok=True)  # py3.8+ supports missing_ok
            except TypeError:
                if tmp_path.exists():
                    tmp_path.unlink()
            return sha_to_rel[digest]

        tmp_path.replace(final_path)
        rel_url = _safe_rel_url(Path(assets_url_prefix) / final_name)
        sha_to_rel[digest] = rel_url
        stats.assets_written += 1
        return rel_url

    def decode_spanning_uri(
        fin,
        mime_main: str,
        already: bytes,
    ) -> Tuple[str, int, bytes]:
        """Decode a data URI whose base64 payload spans beyond current buffer.

        already: bytes that are part of base64 payload already read (from data_start to end of buffer).

        Returns (rel_url, delim_byte_int, remainder_bytes_after_delim).
        """
        # Decode into temp file while searching for first delimiter.
        ext = _guess_extension(mime_main)
        tmp_path = assets_dir / f"._tmp_{uuid.uuid4().hex}{ext}"
        sha = hashlib.sha256()
        decoded_total = 0

        rem_b64 = b""

        def feed_b64_bytes(b: bytes) -> None:
            nonlocal rem_b64, decoded_total
            if not b:
                return
            b = rem_b64 + b
            # Remove whitespace to keep quartet logic stable.
            if b"\n" in b or b"\r" in b or b"\t" in b or b" " in b:
                b = b"".join(b.split())
            keep = len(b) % 4
            if keep:
                rem_b64 = b[-keep:]
                b = b[:-keep]
            else:
                rem_b64 = b""
            if b:
                decoded = binascii.a2b_base64(b)
                out_fp.write(decoded)
                sha.update(decoded)
                decoded_total += len(decoded)

        with tmp_path.open("wb") as out_fp:
            # First, process already-read bytes.
            # These bytes might already include a delimiter; handle that.
            buf = already
            while True:
                delim_pos = -1
                delim_byte = None
                for d in DELIMS:
                    p = buf.find(bytes([d]))
                    if p != -1 and (delim_pos == -1 or p < delim_pos):
                        delim_pos = p
                        delim_byte = d
                if delim_pos != -1:
                    feed_b64_bytes(buf[:delim_pos])
                    remainder = buf[delim_pos + 1 :]
                    break

                # No delim in current buf; decode it all and read more.
                feed_b64_bytes(buf)
                more = fin.read(chunk_size)
                if not more:
                    # Unexpected EOF.
                    remainder = b""
                    delim_byte = ord(b"\"")
                    break
                stats.bytes_read += len(more)
                buf = more

            # Flush remainder base64 quartets.
            if rem_b64:
                pad = (-len(rem_b64)) % 4
                try:
                    decoded = binascii.a2b_base64(rem_b64 + (b"=" * pad))
                except binascii.Error:
                    decoded = b""
                if decoded:
                    out_fp.write(decoded)
                    sha.update(decoded)
                    decoded_total += len(decoded)

        stats.bytes_decoded += decoded_total

        digest = sha.hexdigest()
        final_name = f"{digest}{ext}"
        final_path = assets_dir / final_name

        if digest in sha_to_rel and final_path.exists():
            try:
                tmp_path.unlink(missing_ok=True)
            except TypeError:
                if tmp_path.exists():
                    tmp_path.unlink()
            rel_url = sha_to_rel[digest]
        else:
            tmp_path.replace(final_path)
            rel_url = _safe_rel_url(Path(assets_url_prefix) / final_name)
            sha_to_rel[digest] = rel_url
            stats.assets_written += 1

        return rel_url, int(delim_byte), remainder

    # Only consider a data URI if ';base64,' appears shortly after 'data:'.
    # This avoids false-positives like JS objects with keys named "data:".
    header_scan_limit = 16 * 1024

    with in_path.open("rb") as fin, out_path.open("wb") as fout:
        tail = b""
        last_log = time.time()

        while True:
            chunk = fin.read(chunk_size)
            if not chunk:
                break
            stats.bytes_read += len(chunk)

            buf = tail + chunk
            buf_low = buf.lower()

            out_i = 0
            i = 0
            incomplete_from: Optional[int] = None

            while True:
                # case-insensitive search for 'data:'
                pos = buf_low.find(DATA_PREFIX, i)
                if pos == -1:
                    break

                                # Quick sanity check: the character after 'data:' should be ';' or a mime token char.
                if pos + len(DATA_PREFIX) >= len(buf):
                    incomplete_from = max(pos - 16, 0)
                    break
                nxt = buf_low[pos + len(DATA_PREFIX) : pos + len(DATA_PREFIX) + 1]
                if nxt not in b";abcdefghijklmnopqrstuvwxyz0123456789":
                    i = pos + len(DATA_PREFIX)
                    continue

                # Find base64 marker after pos (case-insensitive), but only within a small window.
                # If we don't do this, a random 'data:' earlier in the buffer can incorrectly pair
                # with a ';base64,' far away.
                scan_end = min(len(buf), pos + header_scan_limit)
                m = buf_low.find(BASE64_MARKER, pos, scan_end)
                if m == -1:
                    # Might be split across chunks.
                    if pos > len(buf) - tail_keep:
                        incomplete_from = max(pos - 16, 0)
                        break
                    i = pos + len(DATA_PREFIX)
                    continue

                # Validate that this looks like a real data URI header.
                # Most real cases have a mime with '/', e.g. data:image/png;...;base64,
                # Some are data:;base64, (no mime).
                hdr = buf_low[pos + len(DATA_PREFIX) : m]
                if not (hdr.startswith(b";") or (b"/" in hdr)):
                    i = pos + len(DATA_PREFIX)
                    continue

                data_start = m + len(BASE64_MARKER)

                # Debug mode: extract only N assets, but still produce a complete output file.
                # Once the limit is reached, we stop extracting and copy the rest unchanged.
                if max_assets is not None and stats.assets_found >= max_assets:
                    fout.write(buf[out_i:])
                    shutil.copyfileobj(fin, fout)
                    return stats, sha_to_rel

                stats.assets_found += 1

                # Find end delimiter for this data URI.
                end = _iter_first_delim(buf, data_start)
                if end == -1:
                    # Data URI spans beyond current buffer; stream-decode.
                    # Write everything up to pos, then decode spanning URI.
                    fout.write(buf[out_i:pos])

                    mime_raw = buf[pos + len(DATA_PREFIX) : m]
                    mime_main = (
                        mime_raw.split(b";", 1)[0]
                        .split(b",", 1)[0]
                        .decode("ascii", "ignore")
                        .strip()
                        .lower()
                    )

                    already = buf[data_start:]
                    rel_url, delim_byte, remainder = decode_spanning_uri(fin, mime_main, already)

                    fout.write(rel_url.encode("utf-8"))
                    fout.write(bytes([delim_byte]))
                    stats.replaced_spans += 1

                    # IMPORTANT: remainder contains bytes already read from fin *after* the data URI.
                    # We must keep scanning it (and writing it) instead of dropping it, otherwise we
                    # will skip any subsequent data URIs inside remainder.
                    buf = remainder
                    buf_low = buf.lower()
                    out_i = 0
                    i = 0
                    incomplete_from = None
                    continue

                # Ensure ';base64,' is inside this same data URI (before its end)
                # by using the end delimiter.
                # If ';base64,' appears after end, it was a false hit.
                if m >= end:
                    i = pos + len(DATA_PREFIX)
                    continue

                # We have a complete base64 data URI inside buf[pos:end]
                fout.write(buf[out_i:pos])

                mime_raw = buf[pos + len(DATA_PREFIX) : m]
                mime_main = (
                    mime_raw.split(b";", 1)[0]
                    .split(b",", 1)[0]
                    .decode("ascii", "ignore")
                    .strip()
                    .lower()
                )

                b64_mv = memoryview(buf)[data_start:end]
                rel_url = write_asset_from_b64(mime_main, b64_mv)

                fout.write(rel_url.encode("utf-8"))
                out_i = end
                i = end
                stats.replaced_spans += 1

            # Write everything except tail.
            if incomplete_from is not None:
                # Write up to incomplete_from; keep from incomplete_from.
                fout.write(buf[out_i:incomplete_from])
                tail = buf[incomplete_from:]
            else:
                # Keep last tail_keep bytes to handle markers split across chunk boundaries.
                if len(buf) > tail_keep:
                    write_upto = len(buf) - tail_keep
                    fout.write(buf[out_i:write_upto])
                    tail = buf[write_upto:]
                else:
                    # Buffer smaller than tail_keep; keep only unwritten bytes.
                    tail = buf[out_i:]

            if log_every_seconds and (time.time() - last_log) >= log_every_seconds:
                last_log = time.time()
                mb_in = stats.bytes_read / (1024 * 1024)
                mb_out = stats.bytes_decoded / (1024 * 1024)
                print(
                    f"[progress] read={mb_in:,.1f} MiB | extracted={stats.assets_written} assets | decoded={mb_out:,.1f} MiB",
                    file=sys.stderr,
                )

        # Flush any remaining tail.
        if tail:
            fout.write(tail)

    return stats, sha_to_rel


def _zip_dir(src_dir: Path, zip_path: Path) -> None:
    """Zip a directory (deterministic order)."""
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src_dir.rglob("*")):
            if p.is_dir():
                continue
            arcname = p.relative_to(src_dir).as_posix()
            z.write(p, arcname)


def serve_directory(out_dir: Path, *, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Serve out_dir until interrupted."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out_dir))

    class ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

    with ReusableThreadingTCPServer((host, port), handler) as httpd:
        actual_host, actual_port = httpd.server_address[:2]
        print(
            f"Serving {out_dir} at http://{actual_host}:{actual_port}/index.html",
            file=sys.stderr,
        )
        print("Press Ctrl+C to stop the server.", file=sys.stderr)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.", file=sys.stderr)



# -----------------------------
# External asset fetching (optional)
# -----------------------------

_ATTR_URL_RE_QUOTED = re.compile(rb'(?i)\b(?:src|href)\s*=\s*(["\'])([^"\']+)\1')
_ATTR_URL_RE_UNQUOTED = re.compile(rb'(?i)\b(?:src|href)\s*=\s*([^\s"\'<>]+)')
_SRCSET_RE = re.compile(rb'(?i)\bsrcset\s*=\s*(["\'])([^"\']+)\1')
_CSS_URL_RE = re.compile(rb'(?i)url\(\s*(["\']?)([^"\')]+)\1\s*\)')
_IMG_TAG_RE = re.compile(rb"(?is)<img\b[^>]*>")
_IMG_LAZY_SRC_RE = re.compile(
    rb'(?i)(?<![A-Za-z0-9_-])(?:data-src|data-lazy-src|data-original)\s*=\s*(["\'])([^"\']+)\1'
)
_IMG_SRC_RE = re.compile(rb'(?i)(?<![A-Za-z0-9_-])src\s*=\s*(["\'])([^"\']*)\1')

STATIC_RUNTIME_GUARD = b"""<script id="html-unembed-runtime-guards">
(function(w,d){
  "use strict";
  function noop(){}
  function queueFn(name){
    var fn = w[name];
    if (typeof fn === "function") return fn;
    fn = function(){ (fn.q = fn.q || []).push(arguments); };
    fn.q = fn.q || [];
    w[name] = fn;
    return fn;
  }

  w.dataLayer = w.dataLayer || [];
  w.gtag = w.gtag || function(){ w.dataLayer.push(arguments); };

  var fbq = queueFn("fbq");
  fbq.loaded = true;
  fbq.version = fbq.version || "2.0";
  w._fbq = w._fbq || fbq;

  var ttName = w.TiktokAnalyticsObject || "ttq";
  w.TiktokAnalyticsObject = ttName;
  var ttq = w[ttName] = w[ttName] || [];
  ttq._env = ttq._env || {};
  ttq._plugins = ttq._plugins || {};
  ttq._ttq_create = ttq._ttq_create || function(){ return ttq; };
  ttq.getPlugin = ttq.getPlugin || function(){ return null; };
  ttq.methods = ttq.methods || ["page","track","identify","instances","debug","on","off","once","ready","alias","group","enableCookie","disableCookie","holdConsent","revokeConsent","grantConsent"];
  ttq.methods.forEach(function(method){
    ttq[method] = ttq[method] || function(){ (ttq.queue = ttq.queue || []).push([method].concat([].slice.call(arguments))); };
  });

  w.CookieConsent = w.CookieConsent || {};
  var cc = w.CookieConsent;
  cc.consent = cc.consent || {necessary:true,preferences:true,statistics:true,marketing:true};
  cc.regulationRegions = cc.regulationRegions || {ccpa:[]};
  cc.userCountry = cc.userCountry || "";
  cc.hasResponse = cc.hasResponse || false;
  ["setCookie","getCookie","runScripts","signalWindowLoad","downloadConfiguration","processPostPonedMutations","dequeueNonAsyncScripts"].forEach(function(name){
    cc[name] = cc[name] || noop;
  });

  w.CookieControl = w.CookieControl || {};
  w.CookieControl.Dialog = w.CookieControl.Dialog || function(){ this.templates = {}; };
  w.CookieControl.Dialog.prototype = w.CookieControl.Dialog.prototype || {};
  w.CookieControl.CSS = w.CookieControl.CSS || function(ele){ this.HTMLElement = ele || d.createElement("div"); };
  w.CookieControl.Cookie = w.CookieControl.Cookie || {};
  w.CookieControl.Cookie.init = w.CookieControl.Cookie.init || noop;

  w.gapi = w.gapi || {};
  w.gapi.loaded_0 = w.gapi.loaded_0 || noop;
  w.gapi.load = w.gapi.load || noop;
  w.gapi.client = w.gapi.client || {};

  cc.updateRegulations = cc.updateRegulations || noop;
  w._oneSignalInitOptions = w._oneSignalInitOptions || {};
  w.OneSignal = w.OneSignal || [];
  if (!w.CSS) w.CSS = {};
  w.CSS.supports = w.CSS.supports || function(){ return false; };

  var originalWarn = w.console && w.console.warn;
  var originalError = w.console && w.console.error;
  var ignoredConsolePattern = /(Cookiebot script is included twice|Scripts that have a dependency on \\[wc-settings, wc-blocks-checkout\\]|GTM4WP|Klaviyo tracking is disabled|Company ID is not defined)/;
  if (originalWarn) {
    w.console.warn = function(){
      var msg = [].slice.call(arguments).join(" ");
      if (ignoredConsolePattern.test(msg)) return;
      return originalWarn.apply(w.console, arguments);
    };
  }
  if (originalError) {
    w.console.error = function(){
      var msg = [].slice.call(arguments).join(" ");
      if (ignoredConsolePattern.test(msg)) return;
      return originalError.apply(w.console, arguments);
    };
  }

  if (w.MutationObserver && !w.MutationObserver.__htmlUnembedPatched) {
    var OriginalMutationObserver = w.MutationObserver;
    var originalObserve = OriginalMutationObserver.prototype.observe;
    OriginalMutationObserver.prototype.observe = function(target, options){
      if (!target || typeof target.nodeType !== "number") return;
      try { return originalObserve.call(this, target, options); } catch (err) { return; }
    };
    OriginalMutationObserver.__htmlUnembedPatched = true;
  }

  w.addEventListener("error", function(event){
    var msg = event && event.message || "";
    if (/(_ttq_create|fbq is not defined|gapi is not defined|CookieConsent is not defined|CookieControl|MutationObserver|windowOnloadTriggered|execStart)/.test(msg)) {
      event.preventDefault();
      return true;
    }
  }, true);
})(window,document);
</script>
"""


def _is_probably_external(raw: str) -> bool:
    s = (raw or "").strip().lower()
    if not s:
        return False
    if s.startswith("data:") or s.startswith("javascript:") or s.startswith("mailto:") or s.startswith("#"):
        return False
    return True


def _normalize_url(raw: str, base_url: Optional[str]) -> Optional[str]:
    """Return absolute URL suitable for download, or None if we can't resolve."""
    raw = (raw or "").strip()
    if not _is_probably_external(raw):
        return None

    if raw.startswith("//"):
        # protocol-relative
        scheme = "https"
        if base_url:
            try:
                scheme = urllib.parse.urlparse(base_url).scheme or "https"
            except Exception:
                scheme = "https"
        return f"{scheme}:{raw}"

    p = urllib.parse.urlparse(raw)
    if p.scheme in ("http", "https"):
        return raw

    # relative
    if base_url:
        return urllib.parse.urljoin(base_url, raw)

    return None


def _iter_candidate_urls_from_bytes(buf: bytes) -> Iterable[bytes]:
    """Yield raw URL byte strings as they appear in HTML/CSS text."""
    for m in _ATTR_URL_RE_QUOTED.finditer(buf):
        yield m.group(2)

    for m in _ATTR_URL_RE_UNQUOTED.finditer(buf):
        yield m.group(1)

    for m in _SRCSET_RE.finditer(buf):
        # srcset: "url1 1x, url2 2x"
        srcset = m.group(2)
        for part in srcset.split(b","):
            part = part.strip()
            if not part:
                continue
            url = part.split(None, 1)[0]
            if url:
                yield url

    for m in _CSS_URL_RE.finditer(buf):
        yield m.group(2)


def promote_lazy_image_sources(html_path: Path, *, verbose: bool = True) -> int:
    """Copy lazy image URLs from data-src-like attributes into src for static browsing."""
    raw = html_path.read_bytes()
    changed = 0

    def repl(match: re.Match[bytes]) -> bytes:
        nonlocal changed
        tag = match.group(0)
        lazy = _IMG_LAZY_SRC_RE.search(tag)
        if not lazy:
            return tag

        lazy_url = lazy.group(2)
        if not lazy_url or lazy_url.startswith((b"#", b"data:")):
            return tag

        src = _IMG_SRC_RE.search(tag)
        if src:
            current = src.group(2).strip()
            if current and not current.startswith(b"data:"):
                return tag
            changed += 1
            return tag[: src.start(2)] + lazy_url + tag[src.end(2) :]

        insert_at = tag.rfind(b">")
        if insert_at == -1:
            return tag
        changed += 1
        return tag[:insert_at] + b' src="' + lazy_url + b'"' + tag[insert_at:]

    rewritten = _IMG_TAG_RE.sub(repl, raw)
    if changed:
        html_path.write_bytes(rewritten)
    if verbose:
        print(f"Lazy image src promoted: {changed}", file=sys.stderr)
    return changed


def inject_static_runtime_guards(html_path: Path, *, verbose: bool = True) -> bool:
    raw = html_path.read_bytes()
    if b'id="html-unembed-runtime-guards"' in raw:
        return False

    head = re.search(rb"(?is)<head\b[^>]*>", raw)
    if head:
        insert_at = head.end()
    else:
        first_script = re.search(rb"(?is)<script\b", raw)
        insert_at = first_script.start() if first_script else 0

    html_path.write_bytes(raw[:insert_at] + b"\n" + STATIC_RUNTIME_GUARD + raw[insert_at:])
    if verbose:
        print("Static runtime guards injected", file=sys.stderr)
    return True


def disable_optional_localhost_scripts(html_path: Path, *, verbose: bool = True) -> int:
    """Disable optional third-party bundles that are not safe after static extraction."""
    raw = html_path.read_bytes()
    disabled = 0

    def repl(match: re.Match[bytes]) -> bytes:
        nonlocal disabled
        tag = match.group(0)
        attrs = match.group(1)
        body = match.group(2)
        if b'data-html-unembed-disabled="true"' in attrs:
            return tag

        reason = None
        if b'id="woo-slg-google-gsi-js"' in attrs or b"id='woo-slg-google-gsi-js'" in attrs:
            reason = b"Google Sign-In bundle is optional"
        elif b'id="Cookiebot"' in attrs or b"id='Cookiebot'" in attrs or b"CookieConsent.updateRegulations" in body:
            reason = b"Cookiebot is stubbed for local static clone"
        elif b"__fbeventsModules" in body:
            reason = b"Facebook Events is stubbed for local static clone"
        elif b"webpackChunk_klaviyo_onsite_modules" in body or b"KLAVIYO_JS_REGEX" in body:
            reason = b"Klaviyo onsite loader is disabled for local static clone"
        elif b'id="wc-cart-fragments-js"' in attrs or b"id='wc-cart-fragments-js'" in attrs:
            reason = b"WooCommerce cart fragments require a PHP AJAX backend"
        elif b"load.ss.aronia-charlottenburg.ro" in body or b"ss.aronia-charlottenburg.ro" in body:
            reason = b"server-side tracking bundle is disabled for local static clone"
        elif b"OneSignalSDK" in body:
            reason = b"OneSignal push scripts are disabled for local static clone"
        elif b"twemoji" in body and b"_wpemojiSettings" in body:
            reason = b"WordPress emoji runtime is disabled for local static clone"
        elif b"analytics.tiktok.com/i18n/pixel/events.js" in body or b'google_tag_manager["rm"]' in body:
            reason = b"TikTok/GTM dynamic loader is stubbed for local static clone"

        if not reason:
            return tag

        disabled += 1
        attrs = re.sub(rb'(?i)\s+type\s*=\s*(["\'])text/javascript\1', b"", attrs)
        attrs += b' type="text/plain" data-html-unembed-disabled="true"'
        return b"<script" + attrs + b">/* disabled for local static clone: " + reason + b" */</script>"

    rewritten = re.sub(rb"(?is)<script\b([^>]*)>(.*?)</script\s*>", repl, raw)
    if disabled:
        html_path.write_bytes(rewritten)
    if verbose:
        print(f"Optional localhost scripts disabled: {disabled}", file=sys.stderr)
    return disabled


def patch_slick_static_options(html_path: Path, *, verbose: bool = True) -> int:
    """Disable Slick ADA setup in the static clone where generated controls can be absent."""
    raw = html_path.read_bytes()
    patched = raw.replace(b".slick({", b".slick({accessibility:false,")
    count = raw.count(b".slick({")
    if patched != raw:
        html_path.write_bytes(patched)
    if verbose:
        print(f"Slick static options patched: {count}", file=sys.stderr)
    return count


def patch_invalid_static_urls(html_path: Path, *, verbose: bool = True) -> int:
    raw = html_path.read_bytes()
    replacements = {
        b'src="nullblank"': b'src="about:blank"',
        b"src='nullblank'": b"src='about:blank'",
    }
    count = 0
    patched = raw
    for old, new in replacements.items():
        count += patched.count(old)
        patched = patched.replace(old, new)
    if patched != raw:
        html_path.write_bytes(patched)
    if verbose:
        print(f"Invalid static URLs patched: {count}", file=sys.stderr)
    return count


def scan_urls_stream(path: Path, *, chunk_size: int = 4 * 1024 * 1024, tail_keep: int = 256 * 1024) -> Dict[bytes, str]:
    """Scan a potentially huge text file and return map {raw_bytes -> raw_str} for candidate URLs."""
    found: Dict[bytes, str] = {}
    tail = b""
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            buf = tail + chunk
            for raw_b in _iter_candidate_urls_from_bytes(buf):
                if raw_b in found:
                    continue
                try:
                    raw_s = raw_b.decode("utf-8", "ignore")
                except Exception:
                    continue
                if _is_probably_external(raw_s):
                    found[raw_b] = raw_s
            if len(buf) > tail_keep:
                tail = buf[-tail_keep:]
            else:
                tail = buf
    return found


_ALLOWED_MIME_PREFIXES = (
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
    "application/json",
    "image/",
    "font/",
    "application/font-",
    "application/octet-stream",  # some CDNs serve fonts/images as octet-stream
)


def _mime_allowed(mime: str) -> bool:
    mime = (mime or "").split(";", 1)[0].strip().lower()
    if not mime:
        return True
    return any(mime.startswith(p) for p in _ALLOWED_MIME_PREFIXES)


def download_external_asset(
    url: str,
    *,
    out_dir: Path,
    externals_dir: str,
    timeout_s: int,
    max_bytes: int,
    user_agent: str,
) -> Optional[str]:
    """Download a single URL to out_dir/externals_dir/... Return relative path or None on skip/failure."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.netloc or "unknown"

    url_hash = hashlib.sha256(url.encode("utf-8", "ignore")).hexdigest()[:16]
    ext = os.path.splitext(parsed.path)[1]
    ext = ext if (ext and len(ext) <= 10) else ""

    target_dir = out_dir / externals_dir / host
    target_dir.mkdir(parents=True, exist_ok=True)

    tmp_path = target_dir / f"{url_hash}.part"
    if tmp_path.exists():
        try:
            tmp_path.unlink()
        except Exception:
            pass

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "*/*",
        },
        method="GET",
    )

    ctype = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp, tmp_path.open("wb") as out:
            ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype and not _mime_allowed(ctype):
                return None

            total = 0
            while True:
                data = resp.read(64 * 1024)
                if not data:
                    break
                total += len(data)
                if total > max_bytes:
                    return None
                out.write(data)
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None
    except Exception:
        return None

    if not ext:
        guessed = _guess_extension(ctype) if ctype else ".bin"
        ext = guessed or ".bin"
    if not ext.startswith("."):
        ext = "." + ext

    final_path = target_dir / f"{url_hash}{ext}"
    if final_path.exists():
        try:
            tmp_path.unlink()
        except Exception:
            pass
    else:
        try:
            tmp_path.replace(final_path)
        except Exception:
            try:
                shutil.copyfile(tmp_path, final_path)
                tmp_path.unlink()
            except Exception:
                return None

    rel = f"{externals_dir}/{host}/{final_path.name}"
    return rel


def rewrite_bytes_stream(in_path: Path, *, replacements: Dict[bytes, bytes], chunk_size: int = 4 * 1024 * 1024) -> None:
    """In-place streaming replacement in a file, safe for large files."""
    if not replacements:
        return

    keys = sorted(replacements.keys(), key=len, reverse=True)
    max_k = max(len(k) for k in keys)
    tail_keep = max_k + 256

    tmp = in_path.with_suffix(in_path.suffix + ".tmp")
    tail = b""
    with in_path.open("rb") as fin, tmp.open("wb") as fout:
        while True:
            chunk = fin.read(chunk_size)
            if not chunk:
                break
            buf = tail + chunk
            for k in keys:
                buf = buf.replace(k, replacements[k])
            if len(buf) > tail_keep:
                fout.write(buf[:-tail_keep])
                tail = buf[-tail_keep:]
            else:
                tail = buf
        if tail:
            for k in keys:
                tail = tail.replace(k, replacements[k])
            fout.write(tail)

    tmp.replace(in_path)



# -----------------------------
# Inline <style> extraction
# -----------------------------

_STYLE_OPEN = b"<style"
_STYLE_CLOSE = b"</style"
_STYLE_BLOCK_RE = re.compile(rb"(?is)<style\b([^>]*)>(.*?)</style\s*>")
_SCRIPT_BLOCK_RE = re.compile(rb"(?is)<script\b[^>]*>.*?</script\s*>")

_MEDIA_ATTR_RE = re.compile(r"(?is)\bmedia\s*=\s*(\"([^\"]*)\"|'([^']*)'|([^\s>]+))")

def _extract_media_attr(open_tag: bytes) -> Optional[str]:
    """Best-effort parse media=\"...\" from a <style ...> opening tag."""
    try:
        s = open_tag.decode("utf-8", "ignore")
    except Exception:
        return None
    m = _MEDIA_ATTR_RE.search(s)
    if not m:
        return None
    val = m.group(2) or m.group(3) or m.group(4) or ""
    val = (val or "").strip()
    return val or None


def _is_inside_spans(pos: int, spans: List[Tuple[int, int]]) -> bool:
    for start, end in spans:
        if pos < start:
            return False
        if start <= pos < end:
            return True
    return False


def _url_to_local_path(raw: str, out_dir: Path) -> Optional[Path]:
    """If raw looks like a local/relative URL and maps to a file under out_dir, return that path."""
    s = (raw or "").strip()
    if not s:
        return None
    # Malformed CSS from some exporters can leave large base64 fragments looking
    # like local URLs. Do not pass impossible paths to the filesystem.
    if len(s) > 2048 or "\x00" in s:
        return None
    low = s.lower()
    if low.startswith(("http://", "https://", "//", "data:", "javascript:", "mailto:", "tel:")):
        return None
    if low.startswith("#"):
        return None

    # Strip query / fragment for filesystem mapping
    base = s.split("?", 1)[0].split("#", 1)[0]
    base = base.lstrip("/")  # treat /a/b as out_dir/a/b
    if not base:
        return None
    if len(base) > 1024:
        return None

    norm = os.path.normpath(base).replace("\\", "/")
    if norm.startswith(".."):
        return None

    return out_dir / norm


def _safe_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _rewrite_local_urls_in_css(css_path: Path, *, out_dir: Path, verbose: bool = False) -> None:
    """
    Inline styles moved into out_dir/styles/*.css often contain URLs like 'assets/..' that were valid from HTML root.
    After extraction, CSS lives in a subfolder, so those URLs must become relative (e.g. '../assets/...').
    We rewrite any url(...) that points to a file that exists under out_dir.
    """
    raw_map = scan_urls_stream(css_path, chunk_size=512 * 1024, tail_keep=64 * 1024)

    repl: Dict[bytes, bytes] = {}
    for raw_b, raw_s in raw_map.items():
        p = _url_to_local_path(raw_s, out_dir)
        if not p or not _safe_exists(p):
            continue

        # Preserve query/fragment suffix
        base = raw_s.split("?", 1)[0].split("#", 1)[0]
        suffix = raw_s[len(base):]  # includes ?... or #...
        try:
            rel_from_css = os.path.relpath(p, css_path.parent).replace("\\", "/")
        except Exception:
            continue
        new_s = rel_from_css + suffix
        repl[raw_b] = new_s.encode("utf-8")

    if repl:
        if verbose:
            print(f"Rewriting local URLs in CSS: {css_path} ({len(repl)} refs)", file=sys.stderr)
        rewrite_bytes_stream(css_path, replacements=repl, chunk_size=512 * 1024)


def extract_style_tags_to_files(
    html_path: Path,
    *,
    out_dir: Path,
    styles_dir: str = "styles",
    styles_url_prefix: str = "styles",
    verbose: bool = True,
    chunk_size: int = 4 * 1024 * 1024,
    tail_keep: int = 256 * 1024,
) -> List[Path]:
    """
    Extract all inline <style>...</style> blocks in html_path into CSS files under out_dir/styles_dir
    and replace them with <link rel=\"stylesheet\" ...> tags.

    Returns list of created CSS file Paths.
    """
    styles_path = out_dir / styles_dir
    styles_path.mkdir(parents=True, exist_ok=True)

    created: List[Path] = []
    tmp = html_path.with_suffix(html_path.suffix + ".styles.tmp")

    n = 0
    tail = b""

    with html_path.open("rb") as fin, tmp.open("wb") as fout:
        while True:
            chunk = fin.read(chunk_size)
            if not chunk:
                break
            buf = tail + chunk
            low = buf.lower()

            out_i = 0
            i = 0
            while True:
                pos = low.find(_STYLE_OPEN, i)
                if pos == -1:
                    break

                fout.write(buf[out_i:pos])

                open_end = buf.find(b">", pos)
                if open_end == -1:
                    # keep from pos in tail
                    tail = buf[pos:]
                    out_i = 0
                    i = 0
                    break

                open_tag = buf[pos:open_end + 1]
                media = _extract_media_attr(open_tag)

                n += 1
                css_name = f"inline-style-{n:03d}.css"
                css_file = styles_path / css_name

                link = f'<link rel="stylesheet" href="{styles_url_prefix}/{css_name}"'
                if media:
                    link += f' media="{media}"'
                link += ">\n"
                fout.write(link.encode("utf-8"))

                content_start = open_end + 1
                close_pos = low.find(_STYLE_CLOSE, content_start)

                with css_file.open("wb") as css_out:
                    if close_pos != -1:
                        css_out.write(buf[content_start:close_pos])

                        close_end = low.find(b">", close_pos)
                        if close_end == -1:
                            # closing tag end spans reads
                            carry = buf[close_pos:]
                            while True:
                                more = fin.read(64 * 1024)
                                if not more:
                                    break
                                b2 = carry + more
                                l2 = b2.lower()
                                ce = l2.find(b">")

                                if ce == -1:
                                    # still not complete close tag; keep small tail
                                    keep = 64 * 1024
                                    if len(b2) > keep:
                                        carry = b2[-keep:]
                                    else:
                                        carry = b2
                                    continue

                                remainder = b2[ce + 1:]
                                buf = remainder
                                low = buf.lower()
                                out_i = 0
                                i = 0
                                break
                        else:
                            remainder = buf[close_end + 1:]
                            buf = remainder
                            low = buf.lower()
                            out_i = 0
                            i = 0
                    else:
                        # close tag spans future reads
                        css_out.write(buf[content_start:])
                        carry = b""
                        while True:
                            more = fin.read(chunk_size)
                            if not more:
                                buf = b""
                                low = b""
                                out_i = 0
                                i = 0
                                break
                            b2 = carry + more
                            l2 = b2.lower()
                            cp = l2.find(_STYLE_CLOSE)
                            if cp == -1:
                                keep = 64 * 1024
                                if len(b2) > keep:
                                    css_out.write(b2[:-keep])
                                    carry = b2[-keep:]
                                else:
                                    carry = b2
                                continue

                            css_out.write(b2[:cp])
                            ce = l2.find(b">", cp)
                            if ce == -1:
                                carry2 = b2[cp:]
                                more2 = fin.read(64 * 1024)
                                if not more2:
                                    break
                                b3 = carry2 + more2
                                l3 = b3.lower()
                                ce2 = l3.find(b">")
                                if ce2 == -1:
                                    break
                                remainder = b3[ce2 + 1:]
                                buf = remainder
                                low = buf.lower()
                                out_i = 0
                                i = 0
                                break

                            remainder = b2[ce + 1:]
                            buf = remainder
                            low = buf.lower()
                            out_i = 0
                            i = 0
                            break

                created.append(css_file)
                # Continue scanning remainder buffer
                continue

            # Write remaining (unprocessed) bytes while keeping a tail
            if len(buf) > tail_keep:
                fout.write(buf[out_i: -tail_keep])
                tail = buf[-tail_keep:]
            else:
                tail = buf[out_i:]

        if tail:
            fout.write(tail)

    tmp.replace(html_path)

    for css_file in created:
        _rewrite_local_urls_in_css(css_file, out_dir=out_dir, verbose=verbose)

    if verbose:
        print(f"Inline <style> extracted: {len(created)}", file=sys.stderr)

    return created


def extract_style_tags_to_files(
    html_path: Path,
    *,
    out_dir: Path,
    styles_dir: str = "styles",
    styles_url_prefix: str = "styles",
    verbose: bool = True,
    chunk_size: int = 4 * 1024 * 1024,
    tail_keep: int = 256 * 1024,
) -> List[Path]:
    """
    Extract real HTML <style> blocks while leaving JavaScript source text alone.

    Some bundled scripts contain literal strings/regexes with '<style'. Treating
    those as tags corrupts JavaScript and can prevent jQuery from loading.
    """
    del chunk_size, tail_keep

    styles_path = out_dir / styles_dir
    styles_path.mkdir(parents=True, exist_ok=True)

    raw = html_path.read_bytes()
    script_spans = [(m.start(), m.end()) for m in _SCRIPT_BLOCK_RE.finditer(raw)]
    scan_raw = bytearray(raw)
    for start, end in script_spans:
        scan_raw[start:end] = b" " * (end - start)

    created: List[Path] = []
    chunks: List[bytes] = []
    cursor = 0

    for match in _STYLE_BLOCK_RE.finditer(bytes(scan_raw)):
        if _is_inside_spans(match.start(), script_spans):
            continue

        chunks.append(raw[cursor:match.start()])
        css_name = f"inline-style-{len(created) + 1:03d}.css"
        css_file = styles_path / css_name
        css_file.write_bytes(match.group(2))

        media = _extract_media_attr(raw[match.start():match.start(2)])
        link = f'<link rel="stylesheet" href="{styles_url_prefix}/{css_name}"'
        if media:
            link += f' media="{media}"'
        link += ">\n"
        chunks.append(link.encode("utf-8"))
        created.append(css_file)
        cursor = match.end()

    if created:
        chunks.append(raw[cursor:])
        html_path.write_bytes(b"".join(chunks))

    for css_file in created:
        _rewrite_local_urls_in_css(css_file, out_dir=out_dir, verbose=verbose)

    if verbose:
        print(f"Inline <style> extracted: {len(created)}", file=sys.stderr)

    return created
def fetch_and_rewrite_externals(
    html_path: Path,
    *,
    out_dir: Path,
    externals_dir: str,
    base_url: Optional[str],
    timeout_s: int,
    max_download_mb: int,
    user_agent: str,
    extra_css_paths: Optional[List[Path]] = None,
    verbose: bool = True,
) -> None:
    """Download external src/href/url() assets referenced in html_path and rewrite links to local files."""
    raw_map = scan_urls_stream(html_path)

    abs_to_rel: Dict[str, str] = {}
    repl: Dict[bytes, bytes] = {}

    max_bytes = max_download_mb * 1024 * 1024

    for raw_b, raw_s in raw_map.items():
        # Skip URLs that already point to a local file we generated (assets/styles/externals...)
        lp = _url_to_local_path(raw_s, out_dir)
        if lp and _safe_exists(lp):
            continue

        abs_url = _normalize_url(raw_s, base_url)
        if not abs_url:
            continue
        if abs_url in abs_to_rel:
            rel = abs_to_rel[abs_url]
        else:
            rel = download_external_asset(
                abs_url,
                out_dir=out_dir,
                externals_dir=externals_dir,
                timeout_s=timeout_s,
                max_bytes=max_bytes,
                user_agent=user_agent,
            )
            if not rel:
                continue
            abs_to_rel[abs_url] = rel

        repl[raw_b] = rel.encode("utf-8")

    if verbose:
        print(f"External assets downloaded: {len(abs_to_rel)} | Rewrites in HTML: {len(repl)}", file=sys.stderr)

    if repl:
        rewrite_bytes_stream(html_path, replacements=repl)

    # Second pass: for each downloaded CSS, fetch url(...) deps inside it (fonts/images).
    css_urls = [u for u in abs_to_rel.keys() if abs_to_rel[u].lower().endswith(".css")]
    for css_url in css_urls:
        css_rel = abs_to_rel[css_url]
        css_path = out_dir / css_rel
        if not css_path.exists():
            continue

        css_raw_map = scan_urls_stream(css_path, chunk_size=512 * 1024, tail_keep=64 * 1024)
        css_repl: Dict[bytes, bytes] = {}

        for raw_b, raw_s in css_raw_map.items():
            lp = _url_to_local_path(raw_s, out_dir)
            if lp and _safe_exists(lp):
                continue

            abs_dep = _normalize_url(raw_s, css_url)
            if not abs_dep:
                continue
            if abs_dep in abs_to_rel:
                dep_rel = abs_to_rel[abs_dep]
            else:
                dep_rel = download_external_asset(
                    abs_dep,
                    out_dir=out_dir,
                    externals_dir=externals_dir,
                    timeout_s=timeout_s,
                    max_bytes=max_bytes,
                    user_agent=user_agent,
                )
                if not dep_rel:
                    continue
                abs_to_rel[abs_dep] = dep_rel

            css_repl[raw_b] = dep_rel.encode("utf-8")

        if css_repl:
            if verbose:
                print(f"  CSS deps rewritten: {css_path} ({len(css_repl)} refs)", file=sys.stderr)
            rewrite_bytes_stream(css_path, replacements=css_repl, chunk_size=512 * 1024)



    # Third pass: also process local CSS files (e.g. extracted from inline <style>) if provided.
    if extra_css_paths:
        for css_path in extra_css_paths:
            if not css_path.exists():
                continue
            css_raw_map = scan_urls_stream(css_path, chunk_size=512 * 1024, tail_keep=64 * 1024)
            css_repl: Dict[bytes, bytes] = {}
            for raw_b, raw_s in css_raw_map.items():
                lp = _url_to_local_path(raw_s, out_dir)
                if lp and _safe_exists(lp):
                    continue
                abs_dep = _normalize_url(raw_s, base_url)
                if not abs_dep:
                    continue
                if abs_dep in abs_to_rel:
                    dep_rel = abs_to_rel[abs_dep]
                else:
                    dep_rel = download_external_asset(
                        abs_dep,
                        out_dir=out_dir,
                        externals_dir=externals_dir,
                        timeout_s=timeout_s,
                        max_bytes=max_bytes,
                        user_agent=user_agent,
                    )
                    if not dep_rel:
                        continue
                    abs_to_rel[abs_dep] = dep_rel
                css_repl[raw_b] = dep_rel.encode("utf-8")

            if css_repl:
                if verbose:
                    print(f"  Local CSS deps rewritten: {css_path} ({len(css_repl)} refs)", file=sys.stderr)
                rewrite_bytes_stream(css_path, replacements=css_repl, chunk_size=512 * 1024)
def main() -> int:
    ap = argparse.ArgumentParser(description="Extract base64 data URIs from a huge HTML file.")
    ap.add_argument("--input", required=True, help="Input HTML file")
    ap.add_argument("--out-dir", required=True, help="Output directory")
    ap.add_argument(
        "--out-html",
        default="index.html",
        help="Output HTML filename inside out-dir (default: index.html)",
    )
    ap.add_argument(
        "--assets-dir",
        default="assets",
        help="Assets folder name inside out-dir (default: assets)",
    )
    ap.add_argument(
        "--extract-style-tags",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Extract inline <style> blocks into CSS files and replace with <link> tags (default: enabled)",
    )
    ap.add_argument(
        "--styles-dir",
        default="styles",
        help="Folder name inside out-dir for extracted inline CSS (default: styles)",
    )

    ap.add_argument(
        "--chunk-size-mb",
        type=int,
        default=4,
        help="Read chunk size in MiB (default: 4)",
    )
    ap.add_argument(
        "--tail-keep-kb",
        type=int,
        default=128,
        help="Keep this many KiB as tail between reads (default: 128)",
    )
    ap.add_argument(
        "--max-assets",
        type=int,
        default=None,
        help="Extract at most N assets (debug/quick test)",
    )
    ap.add_argument(
        "--zip",
        action="store_true",
        help="Also produce out-dir.zip next to out-dir",
    )
    ap.add_argument(
        "--serve-after",
        "--after-done-start-server",
        action="store_true",
        help="Start a local static web server for out-dir after extraction finishes",
    )
    ap.add_argument(
        "--serve-host",
        default="127.0.0.1",
        help="Host for --serve-after/--run-on-web (default: 127.0.0.1)",
    )
    ap.add_argument(
        "--serve-port",
        type=int,
        default=8000,
        help="Port for --serve-after (default: 8000; use 0 for any free port)",
    )
    ap.add_argument(
        "--run-on-web",
        type=int,
        metavar="PORT",
        default=None,
        help="Alias for --serve-after --serve-port PORT",
    )

    ap.add_argument(
        "--fetch-externals",
        action="store_true",
        help="Download external http(s) assets referenced by src/href/url() into local files and rewrite links",
    )
    ap.add_argument(
        "--base-url",
        default=None,
        help="Base URL to resolve relative links (e.g. https://example.com/). If omitted, relative URLs are skipped.",
    )
    ap.add_argument(
        "--externals-dir",
        default="externals",
        help="Folder name inside out-dir for downloaded external assets (default: externals)",
    )
    ap.add_argument(
        "--timeout-s",
        type=int,
        default=20,
        help="HTTP download timeout in seconds (default: 20)",
    )
    ap.add_argument(
        "--max-download-mb",
        type=int,
        default=50,
        help="Max size per downloaded external asset in MiB (default: 50)",
    )
    ap.add_argument(
        "--user-agent",
        default="html-unembed-tool/0.2",
        help="User-Agent header for external downloads",
    )
    args = ap.parse_args()

    if args.run_on_web is not None:
        args.serve_after = True
        args.serve_port = args.run_on_web

    in_path = Path(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    out_html = out_dir / args.out_html
    assets_dir = out_dir / args.assets_dir

    stats, _ = extract_data_uris_stream(
        in_path=in_path,
        out_path=out_html,
        assets_dir=assets_dir,
        assets_url_prefix=args.assets_dir,
        chunk_size=int(args.chunk_size_mb) * 1024 * 1024,
        tail_keep=int(args.tail_keep_kb) * 1024,
        max_assets=args.max_assets,
    )

    print(
        f"Done. Replaced={stats.replaced_spans} | Assets written={stats.assets_written} | "
        f"Read={stats.bytes_read/(1024*1024):,.1f} MiB | Decoded={stats.bytes_decoded/(1024*1024):,.1f} MiB",
        file=sys.stderr,
    )

    inject_static_runtime_guards(out_html, verbose=True)

    style_css_files: List[Path] = []
    if args.extract_style_tags:
        style_css_files = extract_style_tags_to_files(
            html_path=out_html,
            out_dir=out_dir,
            styles_dir=str(args.styles_dir),
            styles_url_prefix=str(args.styles_dir),
            verbose=True,
        )

    if args.fetch_externals:
        fetch_and_rewrite_externals(
            html_path=out_html,
            out_dir=out_dir,
            externals_dir=args.externals_dir,
            base_url=args.base_url,
            timeout_s=int(args.timeout_s),
            max_download_mb=int(args.max_download_mb),
            user_agent=str(args.user_agent),
            extra_css_paths=style_css_files,
            verbose=True,
        )

    disable_optional_localhost_scripts(out_html, verbose=True)
    patch_slick_static_options(out_html, verbose=True)
    patch_invalid_static_urls(out_html, verbose=True)
    promote_lazy_image_sources(out_html, verbose=True)

    if args.zip:
        zip_path = out_dir.with_suffix(out_dir.suffix + ".zip") if out_dir.suffix else Path(str(out_dir) + ".zip")
        _zip_dir(out_dir, zip_path)
        print(f"Zip written: {zip_path}", file=sys.stderr)

    if args.serve_after:
        serve_directory(out_dir, host=str(args.serve_host), port=int(args.serve_port))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
