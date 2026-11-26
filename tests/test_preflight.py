import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from preflight import inspect_archive, main
from run import ActionError

PNG = b"\x89PNG\r\n\x1a\n" + b"data" * 1024


def make_archive(entries, compression=zipfile.ZIP_STORED):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return data.getvalue()


class PreflightTests(unittest.TestCase):
    def test_existing_budget_pass_has_no_subscription_link(self):
        result = inspect_archive(make_archive([("index.html", b"Example")]), 1024)
        self.assertEqual(result["status"], "already_within_budget")
        self.assertNotIn("service_plans_url", result)

    def test_exact_budget_passes(self):
        archive = make_archive([("image.png", PNG)])
        result = inspect_archive(archive, len(archive))
        self.assertEqual(result["status"], "already_within_budget")
        self.assertEqual(result["bytes_to_remove"], 0)

    def test_existing_signature_rules_find_an_image_under_another_extension(self):
        archive = make_archive([("media.bin", PNG), ("target.jpg", b"non-image file")])
        result = inspect_archive(archive, 1024)
        self.assertEqual(result["status"], "candidate_for_optimization")
        self.assertEqual(result["png_jpeg_candidates"], 1)
        self.assertEqual(result["current_candidate_compressed_bytes"], len(PNG))
        self.assertEqual(result["bytes_to_remove"], len(archive) - 1024)
        self.assertIn("/pricing", result["service_plans_url"])

    def test_compressed_and_expanded_byte_sizes_are_distinct(self):
        archive = make_archive([("image.png", PNG), ("manifest", bytes(range(256)))], zipfile.ZIP_DEFLATED)
        result = inspect_archive(archive, 1024)
        self.assertLess(result["current_candidate_compressed_bytes"], len(PNG))
        self.assertEqual(result["complete_zip_bytes"], len(archive))

    def test_reports_large_nonimage_share_without_a_checkout_link(self):
        archive = make_archive([("small.png", PNG), ("bundle.js", b"a" * 16384)])
        result = inspect_archive(archive, 1024)
        self.assertEqual(result["status"], "review_nonimage_bytes_and_zip_overhead")
        self.assertNotIn("service_plans_url", result)

    def test_reports_no_images_with_misleading_extension(self):
        result = inspect_archive(make_archive([("x.jpeg", b"not an image" * 300)]), 1024)
        self.assertEqual(result["status"], "no_png_or_jpeg_candidates")

    def test_invalid_zip_has_no_checkout_result(self):
        with self.assertRaises(ActionError):
            inspect_archive(b"Not a ZIP", 1024)

    def test_cli_reads_the_checkout_fixture_without_mutating_it(self):
        fixture = Path(__file__).parent / "fixtures" / "sample-creative.zip"
        before = fixture.read_bytes()
        with TemporaryDirectory() as work:
            path = Path(work) / "source.zip"
            path.write_bytes(before)
            text = io.StringIO()
            with contextlib.redirect_stdout(text):
                self.assertEqual(main([str(path), "--budget-bytes", "1024"]), 0)
            result = json.loads(text.getvalue())
            self.assertEqual(result["complete_zip_bytes"], len(before))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(work).iterdir()), [path])
