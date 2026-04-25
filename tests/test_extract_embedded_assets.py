import base64
import tempfile
import unittest
from pathlib import Path

from html_unembed_tool.extract_embedded_assets import (
    extract_data_uris_stream,
    extract_style_tags_to_files,
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


if __name__ == "__main__":
    unittest.main()
