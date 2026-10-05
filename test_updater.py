import unittest
from unittest import mock

import updater


def release(tag, assets=("TTSReader-Setup.exe", "TTSReader-Setup.exe.sha256")):
    return {
        "tag_name": tag,
        "body": "notas",
        "html_url": "https://github.com/u/r/releases/tag/" + tag,
        "assets": [
            {"name": name, "browser_download_url": f"https://github.com/u/r/releases/download/{tag}/{name}"}
            for name in assets
        ],
    }


class VersionTests(unittest.TestCase):
    def test_parse_ignores_prefix_and_trailing_zeros(self):
        self.assertEqual(updater.parse_version("v1.2.0"), updater.parse_version("1.2"))
        self.assertEqual(updater.parse_version("v10.0.1"), (10, 0, 1))

    def test_parse_rejects_garbage(self):
        with self.assertRaises(ValueError):
            updater.parse_version("latest")

    def test_is_newer_compares_numerically(self):
        self.assertTrue(updater.is_newer("v1.10.0", "1.9.9"))
        self.assertFalse(updater.is_newer("v1.2.0", "1.2.0"))
        self.assertFalse(updater.is_newer("v1.0.0", "1.2.0"))

    def test_dev_version_is_older_than_any_release(self):
        self.assertTrue(updater.is_newer("v0.0.1", "0.0.0-dev"))


class CheckForUpdateTests(unittest.TestCase):
    def test_returns_none_when_current(self):
        with mock.patch.object(updater, "fetch_latest_release", return_value=release("v1.0.0")):
            self.assertIsNone(updater.check_for_update("u/r", "1.0.0"))

    def test_returns_info_with_assets(self):
        with mock.patch.object(updater, "fetch_latest_release", return_value=release("v1.1.0")):
            info = updater.check_for_update("u/r", "1.0.0")
        self.assertEqual(info.version, "1.1.0")
        self.assertTrue(info.installable)
        self.assertTrue(info.installer_url.endswith("/TTSReader-Setup.exe"))

    def test_release_without_installer_is_not_installable(self):
        with mock.patch.object(updater, "fetch_latest_release", return_value=release("v1.1.0", assets=())):
            info = updater.check_for_update("u/r", "1.0.0")
        self.assertFalse(info.installable)

    def test_non_https_assets_are_ignored(self):
        data = release("v1.1.0")
        for asset in data["assets"]:
            asset["browser_download_url"] = asset["browser_download_url"].replace("https://", "http://")
        with mock.patch.object(updater, "fetch_latest_release", return_value=data):
            self.assertFalse(updater.check_for_update("u/r", "1.0.0").installable)

    def test_bad_tag_raises_update_error(self):
        with mock.patch.object(updater, "fetch_latest_release", return_value=release("nightly")):
            with self.assertRaises(updater.UpdateError):
                updater.check_for_update("u/r", "1.0.0")

    def test_invalid_repo_is_rejected(self):
        with self.assertRaises(updater.UpdateError):
            updater.fetch_latest_release("não é um repo")


if __name__ == "__main__":
    unittest.main()
