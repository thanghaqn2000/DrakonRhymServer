import asyncio
import unittest
from unittest.mock import AsyncMock


class YoutubeExternalDownloadTests(unittest.TestCase):
    def _fresh_chain(self, **kwargs):
        import youtube_external_download as yed

        kwargs.setdefault("health", yed.ProviderHealthRegistry())
        return yed.YouTubeDownloadService(**kwargs)

    def test_provider_chain_uses_next_key_after_credit_error(self):
        import youtube_external_download as yed

        attempts = []

        async def provider_attempt(provider_name, api_key, url, mode):
            attempts.append((provider_name, api_key, mode))
            if api_key == "key-1":
                raise yed.ProviderCreditExhausted(provider_name, "out of credits")
            return yed.DownloadResult(
                path=None,
                provider="youtube",
                title="Video",
                duration=120,
                filesize=None,
                external_provider=provider_name,
                video_id="abc123xyz89",
                thumbnail="https://img.example/thumb.jpg",
                channel="Channel",
                duration_string="2:00",
                view_count=99,
                download_url="https://cdn.example/file.mp4",
                source_ext="mp4",
            )

        chain = self._fresh_chain(provider_attempt=provider_attempt, ytdlp_metadata_fetcher=None, ytdlp_downloader=None)

        result = asyncio.run(chain.fetch_metadata("https://www.youtube.com/watch?v=abc123xyz89"))

        self.assertEqual(result.external_provider, "video-download-api")
        self.assertEqual(
            attempts,
            [
                ("video-download-api", "key-1", "metadata"),
                ("video-download-api", "key-2", "metadata"),
            ],
        )

    def test_provider_chain_falls_back_to_next_provider_after_all_keys_fail(self):
        import youtube_external_download as yed

        attempts = []

        async def provider_attempt(provider_name, api_key, url, mode):
            attempts.append((provider_name, api_key, mode))
            if provider_name == "video-download-api":
                raise yed.ProviderDownloadError(provider_name, "temporary failure")
            return yed.DownloadResult(
                path=None,
                provider="youtube",
                title="Video",
                duration=120,
                filesize=None,
                external_provider=provider_name,
                video_id="abc123xyz89",
                thumbnail=None,
                channel=None,
                duration_string=None,
                view_count=None,
                download_url="https://cdn.example/file.mp4",
                source_ext="mp4",
            )

        chain = self._fresh_chain(provider_attempt=provider_attempt, ytdlp_metadata_fetcher=None, ytdlp_downloader=None)

        result = asyncio.run(chain.fetch_metadata("https://www.youtube.com/watch?v=abc123xyz89"))

        self.assertEqual(result.external_provider, "tunelio")
        self.assertEqual(
            attempts,
            [
                ("video-download-api", "key-1", "metadata"),
                ("video-download-api", "key-2", "metadata"),
                ("tunelio", "tunelio-key", "metadata"),
            ],
        )

    def test_provider_chain_uses_ytdlp_fallback_after_external_failures(self):
        import youtube_external_download as yed

        attempts = []

        async def provider_attempt(provider_name, api_key, url, mode):
            attempts.append((provider_name, api_key, mode))
            raise yed.ProviderDownloadError(provider_name, "down")

        async def ytdlp_metadata_fetcher(url):
            return yed.DownloadResult(
                path=None,
                provider="youtube",
                title="Fallback title",
                duration=91,
                filesize=None,
                external_provider=None,
                video_id="fallback1234",
                thumbnail=None,
                channel="Fallback channel",
                duration_string="1:31",
                view_count=7,
                download_url=None,
                source_ext="webm",
            )

        chain = self._fresh_chain(
            provider_attempt=provider_attempt,
            ytdlp_metadata_fetcher=ytdlp_metadata_fetcher,
            ytdlp_downloader=None,
        )

        result = asyncio.run(chain.fetch_metadata("https://www.youtube.com/watch?v=abc123xyz89"))

        self.assertEqual(result.title, "Fallback title")
        self.assertIsNone(result.external_provider)
        self.assertEqual(
            attempts,
            [
                ("video-download-api", "key-1", "metadata"),
                ("video-download-api", "key-2", "metadata"),
                ("tunelio", "tunelio-key", "metadata"),
                ("captapi", "capt-1", "metadata"),
            ],
        )

    def test_download_audio_falls_back_to_next_provider_when_first_download_url_is_forbidden(self):
        import youtube_external_download as yed

        attempts = []
        first_result = yed.DownloadResult(
            path=None,
            provider="youtube",
            title="Video",
            duration=120,
            filesize=None,
            external_provider="video-download-api",
            video_id="abc123xyz89",
            thumbnail=None,
            channel=None,
            duration_string=None,
            view_count=None,
            download_url="https://cdn.example/blocked.mp4",
            source_ext="mp4",
        )
        second_result = yed.DownloadResult(
            path=None,
            provider="youtube",
            title="Video",
            duration=120,
            filesize=None,
            external_provider="tunelio",
            video_id="abc123xyz89",
            thumbnail=None,
            channel=None,
            duration_string=None,
            view_count=None,
            download_url="https://cdn.example/ok.mp3",
            source_ext="mp3",
        )

        async def provider_attempt(provider_name, api_key, url, mode):
            attempts.append((provider_name, api_key, mode))
            if provider_name == "video-download-api":
                return first_result
            if provider_name == "tunelio":
                return second_result
            raise yed.ProviderDownloadError(provider_name, "unused")

        async def materialize_side_effect(result, workdir, **kwargs):
            if result.external_provider == "video-download-api":
                raise yed.ProviderDownloadError(
                    "video-download-api",
                    "HTTP Error 403: Forbidden",
                    error_kind=yed.ErrorKind.FORBIDDEN,
                )
            materialized = yed.DownloadResult(
                path=workdir / "source.mp3",
                provider=result.provider,
                title=result.title,
                duration=result.duration,
                filesize=12,
                external_provider=result.external_provider,
                video_id=result.video_id,
                thumbnail=result.thumbnail,
                channel=result.channel,
                duration_string=result.duration_string,
                view_count=result.view_count,
                download_url=result.download_url,
                source_ext=result.source_ext,
            )
            return materialized

        chain = self._fresh_chain(provider_attempt=provider_attempt, ytdlp_metadata_fetcher=None, ytdlp_downloader=None)
        chain.materialize_download = AsyncMock(side_effect=materialize_side_effect)

        result = asyncio.run(
            chain.download_audio(
                "https://www.youtube.com/watch?v=abc123xyz89",
                workdir=__import__("pathlib").Path("."),
                req_id="req123",
            )
        )

        self.assertEqual(result.external_provider, "tunelio")
        self.assertEqual(result.download_strategy, "staged_race")
        self.assertIn(("video-download-api", "key-1", "download"), attempts)
        self.assertIn(("tunelio", "tunelio-key", "download"), attempts)
        self.assertNotIn(("video-download-api", "key-2", "download"), attempts)

    def test_cooldown_skips_recently_blocked_provider(self):
        import youtube_external_download as yed

        health = yed.ProviderHealthRegistry()
        health.record_failure(
            "video-download-api",
            "key-1",
            "metadata",
            yed.ErrorKind.FORBIDDEN,
            latency_s=0.1,
        )

        attempts = []

        async def provider_attempt(provider_name, api_key, url, mode):
            attempts.append((provider_name, api_key, mode))
            return yed.DownloadResult(
                path=None,
                provider="youtube",
                title="Video",
                duration=120,
                filesize=None,
                external_provider=provider_name,
                video_id="abc123xyz89",
                thumbnail=None,
                channel=None,
                duration_string=None,
                view_count=None,
                download_url=None,
                source_ext=None,
            )

        chain = self._fresh_chain(
            provider_attempt=provider_attempt,
            ytdlp_metadata_fetcher=None,
            ytdlp_downloader=None,
            health=health,
        )

        result = asyncio.run(chain.fetch_metadata("https://www.youtube.com/watch?v=abc123xyz89"))

        self.assertEqual(result.external_provider, "video-download-api")
        self.assertEqual(attempts[0], ("video-download-api", "key-2", "metadata"))

    def test_source_hint_is_tried_before_provider_chain(self):
        import youtube_external_download as yed

        attempts = []

        async def provider_attempt(provider_name, api_key, url, mode):
            attempts.append((provider_name, api_key, mode))
            raise yed.ProviderDownloadError(provider_name, "should not be needed")

        async def fake_get_hint(video_id):
            return {
                "provider": "captapi",
                "key_fingerprint": "pt-1",
                "download_url": "https://cdn.example/hint.mp3",
                "source_ext": "mp3",
                "title": "Hint title",
            }

        chain = self._fresh_chain(provider_attempt=provider_attempt, ytdlp_metadata_fetcher=None, ytdlp_downloader=None)

        async def materialize_hint(result, workdir, **kwargs):
            return yed.DownloadResult(
                path=workdir / "source.mp3",
                provider="youtube",
                title=result.title,
                duration=None,
                filesize=42,
                external_provider="captapi",
                video_id=result.video_id,
                thumbnail=None,
                channel=None,
                duration_string=None,
                view_count=None,
                download_url=result.download_url,
                source_ext="mp3",
                download_strategy="cache_hint",
            )

        chain.materialize_download = AsyncMock(side_effect=materialize_hint)

        with unittest.mock.patch("youtube_external_download.cache.get_source_hint", new=fake_get_hint):
            result = asyncio.run(
                chain.download_audio(
                    "https://www.youtube.com/watch?v=abc123xyz89",
                    workdir=__import__("pathlib").Path("."),
                    req_id="req-hint",
                )
            )

        self.assertEqual(result.download_strategy, "cache_hint")
        self.assertEqual(attempts, [])


    def test_staged_race_does_not_cancel_slower_provider_when_fast_one_fails(self):
        import youtube_external_download as yed

        attempts = []
        first_result = yed.DownloadResult(
            path=None,
            provider="youtube",
            title="Video",
            duration=120,
            filesize=None,
            external_provider="video-download-api",
            video_id="abc123xyz89",
            thumbnail=None,
            channel=None,
            duration_string=None,
            view_count=None,
            download_url="https://cdn.example/blocked.mp4",
            source_ext="mp4",
        )
        captapi_result = yed.DownloadResult(
            path=None,
            provider="youtube",
            title="Video",
            duration=120,
            filesize=None,
            external_provider="captapi",
            video_id="abc123xyz89",
            thumbnail=None,
            channel=None,
            duration_string=None,
            view_count=None,
            download_url="https://cdn.example/captapi.mp3",
            source_ext="mp3",
        )

        async def provider_attempt(provider_name, api_key, url, mode):
            attempts.append((provider_name, api_key, mode))
            if provider_name == "video-download-api":
                return first_result
            if provider_name == "tunelio":
                raise yed.ProviderDownloadError("tunelio", "error code: 1010", error_kind=yed.ErrorKind.CLOUDFLARE)
            if provider_name == "captapi":
                await asyncio.sleep(0.05)
                return captapi_result
            raise yed.ProviderDownloadError(provider_name, "unused")

        async def materialize_side_effect(result, workdir, **kwargs):
            if result.external_provider == "video-download-api":
                raise yed.ProviderDownloadError(
                    "video-download-api",
                    "HTTP Error 403: Forbidden",
                    error_kind=yed.ErrorKind.FORBIDDEN,
                )
            return yed.DownloadResult(
                path=workdir / "source.mp3",
                provider=result.provider,
                title=result.title,
                duration=result.duration,
                filesize=12,
                external_provider=result.external_provider,
                video_id=result.video_id,
                thumbnail=result.thumbnail,
                channel=result.channel,
                duration_string=result.duration_string,
                view_count=result.view_count,
                download_url=result.download_url,
                source_ext=result.source_ext,
            )

        chain = self._fresh_chain(provider_attempt=provider_attempt, ytdlp_metadata_fetcher=None, ytdlp_downloader=None)
        chain.materialize_download = AsyncMock(side_effect=materialize_side_effect)

        result = asyncio.run(
            chain.download_audio(
                "https://www.youtube.com/watch?v=abc123xyz89",
                workdir=__import__("pathlib").Path("."),
                req_id="req-race",
            )
        )

        self.assertEqual(result.external_provider, "captapi")
        self.assertEqual(result.download_strategy, "staged_race")
        self.assertIn(("captapi", "capt-1", "download"), attempts)


if __name__ == "__main__":
    unittest.main()
