import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import main


class YtDlpAuthArgsTests(unittest.TestCase):
    def setUp(self):
        self.old_cookie_file = getattr(main, "YT_DLP_COOKIES_FILE", "")
        self.old_cookies_from_browser = getattr(main, "YT_DLP_COOKIES_FROM_BROWSER", "")
        self.old_js_runtime = getattr(main, "YT_DLP_JS_RUNTIME", "")

    def tearDown(self):
        main.YT_DLP_COOKIES_FILE = self.old_cookie_file
        main.YT_DLP_COOKIES_FROM_BROWSER = self.old_cookies_from_browser
        main.YT_DLP_JS_RUNTIME = self.old_js_runtime

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

    def test_ytdlp_cmd_enables_deno_js_runtime_by_default(self):
        main.YT_DLP_COOKIES_FILE = ""
        main.YT_DLP_COOKIES_FROM_BROWSER = ""

        cmd = main._yt_dlp_cmd("--skip-download")

        self.assertIn("--js-runtimes", cmd)
        self.assertEqual(cmd[cmd.index("--js-runtimes") + 1], "deno")
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

    def test_ytdlp_403_errors_trigger_hls_fallback(self):
        self.assertTrue(main._should_retry_with_hls_fallback(b"ERROR: HTTP Error 403: Forbidden"))
        self.assertTrue(main._should_retry_with_hls_fallback(b"error: http error 403: forbidden"))
        self.assertTrue(main._should_retry_with_hls_fallback(b"unable to download video data: 403 Forbidden"))
        self.assertTrue(main._should_retry_with_hls_fallback(b"unable to download video data: HTTP Error 403"))
        self.assertFalse(main._should_retry_with_hls_fallback(b"ERROR: Sign in to confirm you're not a bot"))

    def test_ytdlp_download_args_can_use_hls_fallback_format(self):
        args = main._download_audio_args("/tmp/source.%(ext)s", main.YT_DLP_HLS_FALLBACK_FORMAT)

        self.assertIn("-f", args)
        self.assertEqual(args[args.index("-f") + 1], main.YT_DLP_HLS_FALLBACK_FORMAT)
        self.assertIn("-x", args)
        self.assertIn("--audio-format", args)


class DownloadAudioFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_audio_retries_hls_fallback_after_403(self):
        with TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            calls = []

            async def fake_run_ytdlp(args, timeout, req_id, label, cookie_workdir=None):
                calls.append((args, label))
                if len(calls) == 1:
                    return 1, b"", b"ERROR: unable to download video data: HTTP Error 403: Forbidden"
                (workdir / "source.mp3").write_bytes(b"mp3")
                return 0, b"", b""

            with patch.object(main, "_run_ytdlp", side_effect=fake_run_ytdlp):
                result = await main._download_audio("https://www.youtube.com/watch?v=abc", workdir, "req123")

            self.assertEqual(result, workdir / "source.mp3")
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][0][calls[0][0].index("-f") + 1], main.YT_DLP_PRIMARY_FORMAT)
            self.assertEqual(calls[1][0][calls[1][0].index("-f") + 1], main.YT_DLP_HLS_FALLBACK_FORMAT)
            self.assertEqual(calls[1][1], "yt-dlp-hls-fallback")


if __name__ == "__main__":
    unittest.main()
