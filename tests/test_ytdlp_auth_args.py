import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import main


class YtDlpAuthArgsTests(unittest.TestCase):
    def setUp(self):
        self.old_cookie_file = getattr(main, "YT_DLP_COOKIES_FILE", "")
        self.old_cookies_from_browser = getattr(main, "YT_DLP_COOKIES_FROM_BROWSER", "")

    def tearDown(self):
        main.YT_DLP_COOKIES_FILE = self.old_cookie_file
        main.YT_DLP_COOKIES_FROM_BROWSER = self.old_cookies_from_browser

    def test_ytdlp_cmd_includes_configured_cookie_file(self):
        main.YT_DLP_COOKIES_FILE = "/run/secrets/youtube-cookies.txt"
        main.YT_DLP_COOKIES_FROM_BROWSER = ""

        cmd = main._yt_dlp_cmd("--dump-single-json", "https://www.youtube.com/watch?v=abc")

        self.assertEqual(
            cmd[:5],
            [main.sys.executable, "-m", "yt_dlp", "--cookies", "/run/secrets/youtube-cookies.txt"],
        )
        self.assertEqual(cmd[-2:], ["--dump-single-json", "https://www.youtube.com/watch?v=abc"])

    def test_ytdlp_cmd_uses_cookies_from_browser_when_no_file_is_configured(self):
        main.YT_DLP_COOKIES_FILE = ""
        main.YT_DLP_COOKIES_FROM_BROWSER = "chrome"

        cmd = main._yt_dlp_cmd("--skip-download")

        self.assertEqual(cmd[:5], [main.sys.executable, "-m", "yt_dlp", "--cookies-from-browser", "chrome"])
        self.assertEqual(cmd[-1], "--skip-download")

    def test_ytdlp_cmd_prefers_cookie_file_when_both_auth_modes_are_configured(self):
        main.YT_DLP_COOKIES_FILE = "/run/secrets/youtube-cookies.txt"
        main.YT_DLP_COOKIES_FROM_BROWSER = "chrome"

        cmd = main._yt_dlp_cmd("--skip-download")

        self.assertEqual(
            cmd[:5],
            [main.sys.executable, "-m", "yt_dlp", "--cookies", "/run/secrets/youtube-cookies.txt"],
        )
        self.assertNotIn("--cookies-from-browser", cmd)
        self.assertNotIn("chrome", cmd)

    def test_ytdlp_cookie_copy_uses_writable_cookie_file(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source-cookies.txt"
            workdir = Path(tmp) / "work"
            workdir.mkdir()
            source.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

            copied = main._copy_ytdlp_cookies(source, workdir, "abc123")
            cmd = main._yt_dlp_cmd("--skip-download", cookies_file=str(copied))

            self.assertNotEqual(copied, source)
            self.assertEqual(copied.parent, workdir)
            self.assertEqual(copied.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            self.assertEqual(
                cmd[:5],
                [main.sys.executable, "-m", "yt_dlp", "--cookies", str(copied)],
            )


if __name__ == "__main__":
    unittest.main()
