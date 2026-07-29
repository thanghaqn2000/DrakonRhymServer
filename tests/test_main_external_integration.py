import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main
import youtube_external_download as yed


class MainExternalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.old_google_client_id = main.GOOGLE_CLIENT_ID
        main.GOOGLE_CLIENT_ID = ""
        self.client = TestClient(main.app)

    def tearDown(self):
        main.GOOGLE_CLIENT_ID = self.old_google_client_id

    def test_metadata_endpoint_uses_external_service_result(self):
        fake_result = yed.DownloadResult(
            path=None,
            provider="youtube",
            title="External title",
            duration=123,
            filesize=None,
            external_provider="tunelio",
            video_id="abc123xyz89",
            thumbnail="https://img.example/thumb.jpg",
            channel="Channel",
            duration_string="2:03",
            view_count=77,
            download_url=None,
            source_ext=None,
        )

        with patch.object(main, "YOUTUBE_DOWNLOADER", create=True) as downloader:
            downloader.fetch_metadata = AsyncMock(return_value=fake_result)

            response = self.client.get(
                "/api/metadata",
                params={"url": "https://www.youtube.com/watch?v=abc123xyz89"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "title": "External title",
                "channel": "Channel",
                "duration": 123,
                "duration_string": "2:03",
                "thumbnail": "https://img.example/thumb.jpg",
                "video_id": "abc123xyz89",
                "view_count": 77,
            },
        )

    def test_download_endpoint_uses_external_service_file_before_pitch_shift(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp3"
            source.write_bytes(b"source-audio")
            fake_result = yed.DownloadResult(
                path=source,
                provider="youtube",
                title="External title",
                duration=123,
                filesize=len(b"source-audio"),
                external_provider="captapi",
                video_id="abc123xyz89",
                thumbnail=None,
                channel=None,
                duration_string="2:03",
                view_count=None,
                download_url=None,
                source_ext="mp3",
            )

            async def fake_pitch(src, dst, pitch_factor, req_id):
                dst.write_bytes(b"shifted")

            with patch.object(main, "YOUTUBE_DOWNLOADER", create=True) as downloader, \
                patch.object(main, "_apply_pitch_shift", side_effect=fake_pitch):
                downloader.fetch_metadata = AsyncMock(return_value=fake_result)
                downloader.download_audio = AsyncMock(return_value=fake_result)

                response = self.client.get(
                    "/api/download",
                    params={"url": "https://www.youtube.com/watch?v=abc123xyz89", "pitch": 1.2},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["x-cache"], "MISS")
            self.assertEqual(response.headers["x-source-provider"], "captapi")
            self.assertEqual(response.content, b"shifted")


if __name__ == "__main__":
    unittest.main()
