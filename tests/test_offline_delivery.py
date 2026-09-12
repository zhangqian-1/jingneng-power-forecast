"""Unit checks for bundle assembly; no model execution or training."""
import json
from pathlib import Path
import tempfile
import unittest

from build_offline_delivery import build_delivery, sha256


class OfflineDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(__file__).resolve().parents[1]
        self.ci = Path(self.temp.name) / "ci"
        self.output = Path(self.temp.name) / "delivery"
        (self.ci / "delivery").mkdir(parents=True)
        self.output.mkdir()
        # This small byte fixture tests packaging checks, not a Docker image.
        (self.output / "image.tar.gz").write_bytes(b"archive-test-fixture")
        self.release = {
            "image": "example/forecast:test", "platform": "linux/arm64",
            "model": "trend_detail_7station_2025_v1",
            "compose_smoke_test": "passed", "container_recreation_test": "passed",
        }
        (self.ci / "release.json").write_text(json.dumps(self.release))
        response = (self.root / "examples/output_example.json").read_bytes()
        for name in ("smoke_response.json", "import_smoke_response.json"):
            (self.ci / name).write_bytes(response)
        for name in ("image_id_before_export.txt", "image_id_after_import.txt"):
            (self.ci / name).write_text("sha256:" + "a" * 64)
        (self.ci / "delivery/compose.yaml").write_bytes((self.root / "compose.yaml").read_bytes())

    def build(self):
        return build_delivery(self.root, self.ci, self.output)

    def test_bundle_has_image_settings_and_docs_but_no_test_data(self):
        release = self.build()
        self.assertEqual(release["offline_image"]["sha256"], sha256(self.output / "image.tar.gz"))
        self.assertIn("POWER_FORECAST_IMAGE=example/forecast:test", (self.output / ".env").read_text())
        self.assertTrue((self.output / "examples/input_example.json").is_file())
        self.assertTrue((self.output / "README.md").is_file())
        self.assertFalse((self.output / "tests").exists())
        self.assertFalse((self.output / "runtime").exists())
        for line in (self.output / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, sha256(self.output / name))

    def test_missing_image_rejected(self):
        (self.output / "image.tar.gz").unlink()
        with self.assertRaisesRegex(ValueError, "missing or empty"):
            self.build()

    def test_old_delivery_rejected(self):
        (self.output / "old.txt").write_text("old")
        with self.assertRaisesRegex(ValueError, "only the new image"):
            self.build()

    def test_changed_predictions_rejected(self):
        path = self.ci / "import_smoke_response.json"
        response = json.loads(path.read_text(encoding="utf-8"))
        response["data"][0]["predictedPower"] += 1
        path.write_text(json.dumps(response))
        with self.assertRaisesRegex(ValueError, "predictions"):
            self.build()

    def test_wrong_image_rejected(self):
        (self.ci / "image_id_after_import.txt").write_text("sha256:" + "b" * 64)
        with self.assertRaisesRegex(ValueError, "image ID"):
            self.build()

    def test_failed_acceptance_rejected(self):
        self.release["container_recreation_test"] = "failed"
        (self.ci / "release.json").write_text(json.dumps(self.release))
        with self.assertRaisesRegex(ValueError, "did not pass"):
            self.build()

    def test_changed_compose_rejected(self):
        (self.ci / "delivery/compose.yaml").write_text("services: {}")
        with self.assertRaisesRegex(ValueError, "Compose"):
            self.build()


if __name__ == "__main__":
    unittest.main()
