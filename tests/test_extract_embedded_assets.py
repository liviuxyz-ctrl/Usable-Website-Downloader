import base64
import tempfile
import unittest
from pathlib import Path

from html_unembed_tool.extract_embedded_assets import (
    disable_optional_localhost_scripts,
    extract_data_uris_stream,
    extract_style_tags_to_files,
    inject_static_runtime_guards,
    patch_invalid_static_urls,
    patch_slick_static_options,
    promote_lazy_image_sources,
    _url_to_local_path,
)


class ExtractEmbeddedAssetsTests(unittest.TestCase):
    def test_extracts_base64_data_uri_without_loading_whole_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source = root / "input.html"
            output = root / "out" / "index.html"
            assets = root / "out" / "assets"
            payload = base64.b64encode(b"hello from embedded data").decode("ascii")

            output.parent.mkdir()
            source.write_text(
                f'<a download href="data:text/plain;base64,{payload}">file</a>',
                encoding="utf-8",
            )

            stats, asset_map = extract_data_uris_stream(source, output, assets)

            rewritten = output.read_text(encoding="utf-8")
            self.assertEqual(stats.assets_found, 1)
            self.assertEqual(stats.assets_written, 1)
            self.assertEqual(len(asset_map), 1)
            self.assertNotIn("data:text/plain;base64", rewritten)
            self.assertIn("assets/", rewritten)
            self.assertEqual(
                next(assets.glob("*.txt")).read_bytes(),
                b"hello from embedded data",
            )

    def test_extracts_inline_style_blocks_to_css_files(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            html = out_dir / "index.html"
            html.write_text(
                "<html><head><style media=\"screen\">body{color:red}</style></head></html>",
                encoding="utf-8",
            )

            created = extract_style_tags_to_files(html, out_dir=out_dir, verbose=False)

            self.assertEqual(len(created), 1)
            self.assertEqual(created[0].read_text(encoding="utf-8"), "body{color:red}")
            rewritten = html.read_text(encoding="utf-8")
            self.assertIn('href="styles/inline-style-001.css"', rewritten)
            self.assertIn('media="screen"', rewritten)
            self.assertNotIn("<style", rewritten)

    def test_does_not_extract_style_text_inside_script(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            html = out_dir / "index.html"
            html.write_text(
                '<script>var re=/<style|script>/;</script><style>body{color:red}</style>',
                encoding="utf-8",
            )

            created = extract_style_tags_to_files(html, out_dir=out_dir, verbose=False)

            rewritten = html.read_text(encoding="utf-8")
            self.assertEqual(len(created), 1)
            self.assertIn("var re=/<style|script>/;", rewritten)
            self.assertIn('href="styles/inline-style-001.css"', rewritten)

    def test_skips_absurd_css_url_paths(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            raw = "assets/file.woff" + ("A" * 5000)

            self.assertIsNone(_url_to_local_path(raw, out_dir))

    def test_promotes_lazy_image_data_src_to_src(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            html = Path(tmp_dir) / "index.html"
            html.write_text(
                '<img class="lazy" src="data:image/svg+xml,%3Csvg%3E" data-src="externals/icon.svg">',
                encoding="utf-8",
            )

            changed = promote_lazy_image_sources(html, verbose=False)

            self.assertEqual(changed, 1)
            self.assertIn('src="externals/icon.svg"', html.read_text(encoding="utf-8"))

    def test_injects_runtime_guards_before_existing_scripts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            html = Path(tmp_dir) / "index.html"
            html.write_text("<html><head></head><body><script>fbq('track')</script></body></html>", encoding="utf-8")

            changed = inject_static_runtime_guards(html, verbose=False)

            rewritten = html.read_text(encoding="utf-8")
            self.assertTrue(changed)
            self.assertLess(rewritten.index("html-unembed-runtime-guards"), rewritten.index("fbq('track')"))

    def test_disables_optional_google_sign_in_bundle(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            html = Path(tmp_dir) / "index.html"
            html.write_text(
                '<script type="text/javascript" id="woo-slg-google-gsi-js">bad bundle</script>',
                encoding="utf-8",
            )

            changed = disable_optional_localhost_scripts(html, verbose=False)

            rewritten = html.read_text(encoding="utf-8")
            self.assertEqual(changed, 1)
            self.assertIn('type="text/plain"', rewritten)
            self.assertIn('data-html-unembed-disabled="true"', rewritten)

    def test_disables_optional_third_party_bundles(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            html = Path(tmp_dir) / "index.html"
            html.write_text(
                '<script>CookieConsent.updateRegulations()</script>'
                '<script>window.__fbeventsModules={}</script>'
                '<script>self.webpackChunk_klaviyo_onsite_modules=[]</script>'
                '<script id="wc-cart-fragments-js">post()</script>'
                '<script>var u="https://load.ss.aronia-charlottenburg.ro"</script>'
                '<script>OneSignalSDK()</script>'
                '<script>window._wpemojiSettings={}; twemoji.parse()</script>'
                '<script>google_tag_manager["rm"]["31033517"](28)</script>',
                encoding="utf-8",
            )

            changed = disable_optional_localhost_scripts(html, verbose=False)

            rewritten = html.read_text(encoding="utf-8")
            self.assertEqual(changed, 8)
            self.assertEqual(rewritten.count('data-html-unembed-disabled="true"'), 8)

    def test_patches_slick_accessibility_for_static_clone(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            html = Path(tmp_dir) / "index.html"
            html.write_text("<script>jQuery('.slider').slick({dots:true});</script>", encoding="utf-8")

            changed = patch_slick_static_options(html, verbose=False)

            self.assertEqual(changed, 1)
            self.assertIn("slick({accessibility:false,dots:true}", html.read_text(encoding="utf-8"))

    def test_patches_invalid_static_urls(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            html = Path(tmp_dir) / "index.html"
            html.write_text('<iframe src="nullblank"></iframe>', encoding="utf-8")

            changed = patch_invalid_static_urls(html, verbose=False)

            self.assertEqual(changed, 1)
            self.assertIn('src="about:blank"', html.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
