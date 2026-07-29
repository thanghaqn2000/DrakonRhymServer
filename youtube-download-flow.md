# YouTube Download Flow — DrakonRhymServer

Tài liệu này mô tả **đầy đủ nhưng ngắn gọn** flow tải YouTube audio hiện tại của DrakonRhymServer, để engineer ở dự án khác có thể triển khai lại cùng behavior.

## Mục tiêu

Nhận một **YouTube public video URL**, tải audio về server thành file local chuẩn `source.mp3`, trả metadata cần thiết cho pipeline pitch-shift phía sau, đồng thời xử lý tốt các case:

- bot-block từ YouTube
- provider external hết credit
- video quá dài
- file tải về rỗng hoặc không hợp lệ

## Phạm vi

Tài liệu này chỉ mô tả **flow download YouTube audio** hiện tại.

Không đi sâu vào:

- pipeline pitch-shift / cache phía sau
- flow Facebook (chưa hỗ trợ)
- UI frontend chi tiết

## Entry points hiện tại

Downloader được dùng ở 2 endpoint backend:

1. `GET /api/metadata?url=...`
   - lấy metadata video (title, duration, thumbnail, ...)
   - **không** consume quota

2. `GET /api/download?url=...&pitch=...`
   - tải audio, áp dụng pitch shift, trả file MP3
   - consume daily quota

Core service dùng chung:

- `youtube_external_download.py`
- `cache.py`

Hàm public chính:

```python
# Trong YouTubeDownloadService
async def fetch_metadata(url: str) -> DownloadResult
async def download_audio(url: str, workdir: Path, req_id: str) -> DownloadResult
```

## Hành vi tổng quát

Khi URL được nhận diện là YouTube, hệ thống tải theo thứ tự:

1. Cache hint (nếu có source hint từ lần tải trước)
2. `Video Download API`
3. `Tunelio`
4. `Captapi`
5. `yt-dlp`

Nói cách khác: **ưu tiên external providers trước**, chỉ fallback sang `yt-dlp` khi tất cả external providers fail.

## Step-by-step flow

### 1. Validate URL

Trước khi download, backend validate URL qua `_is_valid_youtube_url()`:

- scheme phải là `http` hoặc `https`
- host phải thuộc danh sách `ALLOWED_HOSTS`

Các dạng YouTube hiện hỗ trợ:

- `https://www.youtube.com/watch?v=...`
- `https://youtu.be/...`
- `https://www.youtube.com/shorts/...`
- `https://www.youtube.com/embed/...`
- `https://music.youtube.com/watch?v=...`
- `https://m.youtube.com/watch?v=...`

### 2. Source hint cache (Redis)

Trước khi gọi external provider, hệ thống kiểm tra Redis xem có source hint cho video này không:

- Key: `source_hint:{video_id}`
- TTL: `DRAKON_SOURCE_HINT_TTL_SECONDS` (mặc định 1800s = 30 phút)
- Lưu: provider, key fingerprint, download_url, source_ext, title

Nếu có hint hợp lệ, hệ thống dùng thẳng download_url đó mà không cần goi API provider. Nếu fail, hint bị xoá.

### 3. Provider chain cho download

#### Stage 1 — Best single attempt

Chọn provider có score cao nhất, thử download. Nếu thành công → return ngay.

#### Stage 2 — Staged race

Nếu stage 1 fail, chạy song song tối đa `STAGE_RACE_FAMILY_COUNT` provider family khác nhau (mỗi family 1 key). Mỗi attempt chạy trong thư mục riêng để tránh ghi đè. Family nào trả kết quả trước sẽ được chọn. Các attempt bị cancel sẽ được retry ở stage 3.

Thời gian chờ race: `DRAKON_STAGE_RACE_DELAY_SECONDS` (mặc định 3s).

#### Stage 3 — Remaining attempts

Các attempt còn lại (bao gồm cả attempt bị cancel ở stage 2) được thử lần lượt theo scored order.

#### Fallback — yt-dlp

Nếu tất cả external providers fail, hệ thống fallback sang yt-dlp.

### 4. Provider chain cho metadata

Đơn giản hơn: thử lần lượt từng provider theo scored order. Nếu tất cả fail → fallback yt-dlp.

### 5. Provider health tracking

Mỗi provider key được theo dõi:

- `last_success_at`, `last_failure_at`
- `failure_streak`
- `cooldown_until` (dựa trên loại lỗi)
- `avg_latency_ms`

Scoring ưu tiên provider vừa success gần đây, phạt provider đang trong cooldown hoặc có nhiều failure.

Cooldown theo loại lỗi:

| Error kind | Cooldown |
|---|---|
| Cloudflare | 120s |
| Forbidden | 60s |
| Timeout | 30s |
| Bot blocked | 180s |
| Empty response | 45s |
| Unavailable | 90s |
| Credits | 0s (không cooldown, chuyển key ngay) |

### 6. Cách xử lý provider hết credit

Đây là behavior quan trọng của flow hiện tại.

Khi external provider trả dấu hiệu hết credit, hệ thống **không fail ngay**.

Các tín hiệu được coi là hết credit:

- HTTP `402`
- response chứa: `insufficient_credits`, `payment_required`, `out of credits`, `not enough credits`, `no credits`

Behavior:

1. thử API key tiếp theo của cùng provider (nếu có multi-key)
2. nếu hết key thì chuyển sang provider tiếp theo
3. nếu toàn bộ external chain fail thì mới fallback sang `yt-dlp`

### 7. Download file qua external provider

Khi một external provider resolve thành công `download_url`, hệ thống:

- kiểm tra scheme URL chỉ chấp nhận `http`/`https`
- stream download bằng HTTP GET
- ghi xuống file trong `workdir`
- tên file đích: `source.{ext}` (ext từ provider hoặc `mp3`)
- đọc theo chunk `1MB`
- timeout: 30s

Sau khi tải xong:

- nếu file rỗng hoặc size `<= 0` -> fail
- lưu source hint vào Redis

### 8. Fallback sang yt-dlp

Nếu tất cả external providers đều fail, hệ thống gọi `_download_audio_with_ytdlp()`.

#### yt-dlp options chính

```python
{
  "format": "bestaudio/best",            # DRAKON_YT_DLP_PRIMARY_FORMAT
  "extract_audio": True,
  "audio_format": "mp3",
  "audio_quality": "0",
  "outtmpl": "source.%(ext)s",
  "noplaylist": True,
  "socket_timeout": 30,
  "retries": 2,
  "match_filter": "duration<=420",       # DRAKON_MAX_DURATION_SECONDS
}
```

#### HLS fallback

Khi format chính (`bestaudio/best`) trả về lỗi HTTP 403, hệ thống tự động
thử lại với format HLS thấp hơn (`91/92/93/94/95/96`), vốn ít bị YouTube chặn hơn.
Có thể cấu hình qua env:

- `DRAKON_YT_DLP_PRIMARY_FORMAT` (mặc định: `bestaudio/best`)
- `DRAKON_YT_DLP_HLS_FALLBACK_FORMAT` (mặc định: `91/92/93/94/95/96`)

#### Cookie strategy

Env:

- `DRAKON_YT_DLP_COOKIES_FILE` — đường dẫn đến file cookies.txt (Netscape format)
- `DRAKON_YT_DLP_COOKIES_FROM_BROWSER` — tên browser để lấy cookies (vd: `chrome`, `firefox`, `edge`)

Cơ chế:

1. Nếu `DRAKON_YT_DLP_COOKIES_FILE` được set, file cookies được **copy** sang thư mục tạm
   (per-request) trước khi chạy yt-dlp, sau đó tự động xoá. Điều này tránh conflict khi
   nhiều request chạy đồng thời. Đường dẫn copy: `{workdir}/yt_dlp_cookies_{req_id}.txt`.
2. Nếu `DRAKON_YT_DLP_COOKIES_FROM_BROWSER` được set (và không có cookies file),
   yt-dlp dùng `--cookies-from-browser` để lấy cookies từ browser profile.
3. Nếu cả 2 đều trống, yt-dlp chạy anonymous — dễ bị YouTube chặn.

#### JS Runtime

yt-dlp cần JS runtime để giải mã YouTube signature challenge. Cấu hình qua:

- `DRAKON_YT_DLP_JS_RUNTIME` (mặc định: `deno`)

Docker image đã cài sẵn Deno.

### 9. Giới hạn hiện tại

Giới hạn hard-coded:

- `MAX_DURATION_SECONDS = 420` (7 phút) — cấu hình qua `DRAKON_MAX_DURATION_SECONDS`
- Giới hạn này được enforce ở 2 lớp:
  - API `/api/download`: kiểm tra duration trước khi download (từ metadata provider hoặc yt-dlp probe)
  - yt-dlp `--match-filter`: defence-in-depth
- Ngoài ra còn có rate limit:
  - `DRAKON_RATE_LIMIT_PER_DAY` (mặc định 20): số lần download tối đa/ngày/user
  - `DRAKON_MAX_CONCURRENT` (mặc định 2): số request download đồng thời tối đa

Behavior:

- nếu metadata provider trả duration và duration vượt ngưỡng → fail ngay (không consume quota)
- nếu duration = None (provider không trả duration) → reject với lỗi "Could not determine video duration"
- với `yt-dlp`, backend gọi `_probe_duration_seconds_with_ytdlp` để check duration sau khi download

Message user-facing khi vượt giới hạn:

```text
Only videos under 7 minutes are allowed.
```

### 10. Output contract (DownloadResult)

```python
@dataclass
class DownloadResult:
    path: Path | None        # Local path to audio file
    provider: str            # Always "youtube"
    title: str | None
    duration: int | None
    filesize: int | None
    external_provider: str | None  # e.g. "video-download-api"
    video_id: str | None
    thumbnail: str | None
    channel: str | None
    duration_string: str | None
    view_count: int | None
    download_url: str | None
    source_ext: str | None
    download_strategy: str | None   # "scored", "staged_race", "cache_hint", "ytdlp_fallback"
    resolve_seconds: float | None
    materialize_seconds: float | None
```

### 11. Error mapping

Flow hiện tại không expose raw exception ra user.

Một số mapping quan trọng:

#### YouTube bot block

Nếu lỗi có dấu hiệu:

- `not a bot`
- `sign in to confirm`
- `confirm you're not a bot`
- `the page needs to be reloaded`

thì user nhận:

```text
YouTube blocked downloads from this server. Please try again later.
```

#### Provider error mapping (`_download_error_detail`)

| Error kind | User-facing message |
|---|---|
| `FATAL_USER` | This video is unavailable or cannot be downloaded. |
| `CLOUDFLARE`, `BOT_BLOCKED` | Temporary upstream block while fetching audio. Please try again shortly. |
| `TIMEOUT` | Download timed out while contacting upstream providers. Please try again. |
| `UNAVAILABLE` | Source audio is temporarily unavailable. Please try again later. |
| Other | Failed to download audio from the given URL. |

### 12. Environment variables cần mang sang dự án khác

#### External providers

```text
VIDEO_DOWNLOAD_API_KEY_1
VIDEO_DOWNLOAD_API_KEY_2
VIDEO_DOWNLOAD_API_KEY_3
VIDEO_DOWNLOAD_API_KEY_4

TUNELIO_API_KEY

CAPTAPI_API_KEY_1
CAPTAPI_API_KEY_2
CAPTAPI_API_KEY_3
CAPTAPI_API_KEY_4
```

Legacy optional:

```text
VIDEO_DOWNLOAD_API_KEY
CAPTAPI_API_KEY
```

#### yt-dlp fallback

```text
DRAKON_YT_DLP_COOKIES_FILE
DRAKON_YT_DLP_COOKIES_FROM_BROWSER
DRAKON_YT_DLP_JS_RUNTIME=deno
DRAKON_YT_DLP_PRIMARY_FORMAT=bestaudio/best
DRAKON_YT_DLP_HLS_FALLBACK_FORMAT=91/92/93/94/95/96
```

#### Limits

```text
DRAKON_MAX_DURATION_SECONDS=420
DRAKON_RATE_LIMIT_PER_DAY=20
DRAKON_MAX_CONCURRENT=2
```

#### Redis (recommended)

```text
REDIS_URL=redis://localhost:6379
DRAKON_CACHE_TTL_SECONDS=86400
DRAKON_SOURCE_HINT_TTL_SECONDS=1800
```

### 13. Pseudocode triển khai

```python
# YouTubeDownloadService.download_audio (simplified)
async def download_audio(url, workdir, req_id):
    # Try source hint from Redis
    video_id = extract_video_id(url)
    if video_id:
        hinted = await try_source_hint(video_id, workdir, req_id)
        if hinted:
            return hinted

    # Schedule attempts by health score
    attempts = schedule_attempts("download")
    if not attempts:
        return await ytdlp_fallback(url, workdir, req_id)

    # Stage 1: best single attempt
    result = await execute_download(attempts[0], url, workdir, req_id)
    if result:
        return result

    # Stage 2: staged race
    family_leaders = get_best_per_family(attempts[1:])
    winner = await staged_race(family_leaders, url, workdir, req_id)
    if winner:
        return winner

    # Stage 3: remaining attempts
    for spec in remaining_attempts:
        result = await execute_download(spec, url, workdir, req_id)
        if result:
            return result

    # Fallback to yt-dlp
    return await ytdlp_fallback(url, workdir, req_id)
```

### 14. Những điểm không nên bỏ qua khi clone flow

- Phải có **provider chain + credit-aware fallback**, không chỉ gọi một provider duy nhất.
- Phải có **multi-key retry** cho từng provider.
- Phải có **health tracking + cooldown** để tránh gọi provider đang lỗi liên tục.
- Phải có **staged race** để tận dụng nhiều provider song song.
- Phải có **yt-dlp fallback**, nếu không tỷ lệ fail thực tế sẽ cao.
- Phải có **cookie strategy** cho `yt-dlp`.
- Phải có **scheme validation** khi mở URL từ provider (chỉ http/https).
- Phải có **duration limits** để tránh video quá dài.
- Phải map lỗi sang message thân thiện; không nên trả raw lỗi provider/yt-dlp cho user.

### 15. File code gốc để tham chiếu

- `youtube_external_download.py` — core download service
- `main.py` — FastAPI endpoints, yt-dlp integration
- `cache.py` — Redis cache + source hint
- `tests/test_youtube_external_download.py`
- `tests/test_main_external_integration.py`