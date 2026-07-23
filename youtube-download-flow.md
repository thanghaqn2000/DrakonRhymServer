# YouTube Download Flow — DrakonSub

Tài liệu này mô tả **đầy đủ nhưng ngắn gọn** flow tải video YouTube hiện tại của DrakonSub, để engineer ở dự án khác có thể triển khai lại cùng behavior.

## Mục tiêu

Nhận một **YouTube public video URL**, tải video về server thành file local chuẩn `input.mp4`, trả metadata cần thiết cho pipeline phía sau, đồng thời xử lý tốt các case:

- bot-block từ YouTube
- provider external hết credit
- video quá dài / quá nặng
- file tải về không tương thích cho bước xử lý video tiếp theo

## Phạm vi

Tài liệu này chỉ mô tả **flow download YouTube** hiện tại.

Không đi sâu vào:

- pipeline tạo subtitle / dịch / render phía sau
- flow Facebook
- UI frontend chi tiết

## Entry points hiện tại

Downloader được dùng lại ở 2 flow backend:

1. `POST /api/jobs/from-url`
   - tạo job tải video từ URL
   - tải xong mới cho user bấm xử lý subtitle tiếp

2. `POST /api/voiceover/script-jobs/from-url`
   - tải video trước
   - sau đó đi tiếp sang pipeline voiceover/script

Core service dùng chung:

- `auto_subtitle/url_import_service.py`
- `auto_subtitle/youtube_external_download.py`

Hàm public chính:

```python
download_video_from_url(url: str, output_dir: str | Path, output_filename="input.mp4") -> dict
```

## Hành vi tổng quát

Khi URL được nhận diện là YouTube, hệ thống tải theo thứ tự:

1. `Video Download API`
2. `Tunelio`
3. `Captapi`
4. `yt-dlp`

Nói cách khác: **ưu tiên external providers trước**, chỉ fallback sang `yt-dlp` khi tất cả external providers fail.

## Step-by-step flow

### 1. Validate URL

Trước khi download, backend validate:

- URL không được rỗng
- scheme phải là `http` hoặc `https`
- host không được là localhost / private IP / loopback / `.local`
- URL phải là **YouTube video URL hợp lệ**

Các dạng YouTube hiện hỗ trợ:

- `https://www.youtube.com/watch?v=...`
- `https://youtu.be/...`
- `https://www.youtube.com/shorts/...`
- `https://www.youtube.com/embed/...`

Các link không phải video trực tiếp như homepage / channel / unsupported page sẽ bị reject.

Hàm liên quan:

- `validate_video_url()`
- `detect_provider()`
- `validate_url_with_selected_provider()`

## 2. Chuẩn bị output directory

Trước mỗi lần tải:

- tạo `output_dir` nếu chưa có
- cleanup file rác / file dở dang

Pattern cleanup:

- `input.*`
- `*.part`
- `*.ytdl`

Mục tiêu là tránh download cũ làm hỏng lần tải mới.

## 3. Thử external provider chain

Flow YouTube dùng `_try_youtube_external_cascade()`.

Provider order hiện tại:

1. `video-download-api`
2. `tunelio`
3. `captapi`

Danh sách provider thực sự được bật tùy theo env key nào đang tồn tại.

### 3.1 Video Download API

Provider id:

```text
video-download-api
```

Endpoint resolve:

```text
https://p.savenow.to/api/v2/download
```

Behavior:

- request tạo download job
- nếu response chưa có `url`, provider trả `progress_url`
- backend poll `progress_url` mỗi 2 giây
- timeout poll mặc định: `90s`

Env keys:

- `VIDEO_DOWNLOAD_API_KEY_1`
- `VIDEO_DOWNLOAD_API_KEY_2`
- `VIDEO_DOWNLOAD_API_KEY_3`
- `VIDEO_DOWNLOAD_API_KEY_4`

Legacy compatible:

- `VIDEO_DOWNLOAD_API_KEY`

### 3.2 Tunelio

Provider id:

```text
tunelio
```

API flow:

1. gọi `/info`
2. chọn quality
3. gọi `/create`
4. lấy `download_url`

Quality preference hiện tại:

```text
720p -> 480p -> 360p -> 240p -> 144p
```

Nếu quality mong muốn không có, hệ thống tự chọn mức phù hợp tiếp theo.

Env key:

- `TUNELIO_API_KEY`

### 3.3 Captapi

Provider id:

```text
captapi
```

Endpoint resolve:

```text
https://api.captapi.com/v1/youtube/video-download
```

Env keys:

- `CAPTAPI_API_KEY_1`
- `CAPTAPI_API_KEY_2`
- `CAPTAPI_API_KEY_3`
- `CAPTAPI_API_KEY_4`

Legacy compatible:

- `CAPTAPI_API_KEY`

## 4. Cách xử lý provider hết credit

Đây là behavior quan trọng của flow hiện tại.

Khi external provider trả dấu hiệu hết credit, hệ thống **không fail ngay**.

Các tín hiệu được coi là hết credit:

- HTTP `402`
- error/code như `insufficient_credits`, `payment_required`
- message chứa:
  - `out of credits`
  - `not enough credits`
  - `no credits`

Behavior:

1. thử API key tiếp theo của cùng provider
2. nếu hết key thì chuyển sang provider tiếp theo
3. nếu toàn bộ external chain fail thì mới fallback sang `yt-dlp`

## 5. Download file qua external provider

Khi một external provider resolve thành công `download_url`, hệ thống:

- stream download bằng HTTP GET
- ghi xuống file trong `output_dir`
- mặc định tên file đích là `input.mp4`
- đọc theo chunk `1MB`

Sau khi tải xong:

- nếu file rỗng hoặc size `<= 0` -> fail
- trả metadata:
  - `path`
  - `provider = "youtube"`
  - `title`
  - `duration`
  - `filesize`
  - `external_provider`

## 6. Fallback sang yt-dlp

Nếu tất cả external providers đều fail, hệ thống gọi `_download_youtube_with_ytdlp()`.

### yt-dlp options chính

```python
{
  "format": "bv*+ba/b[ext=mp4]/b",
  "merge_output_format": "mp4",
  "outtmpl": "input.%(ext)s",
  "noplaylist": True,
  "max_filesize": MAX_FILE_BYTES,
  "socket_timeout": 30,
  "quiet": True,
  "no_warnings": True,
  "nocheckcertificate": False,
}
```

### Cookie strategy

#### Ưu tiên 1: server cookie file

Env:

- `YT_DLP_COOKIES_FILE`

Nếu có:

- copy file cookies sang `/tmp/drakonsub-youtube-cookies.txt`
- dùng file này cho `yt-dlp`
- đồng thời set extractor args tối ưu hơn cho YouTube:

```python
{
  "youtube": {
    "player_client": ["tv", "web"],
    "player_skip": ["webpage"],
  }
}
```

#### Ưu tiên 2: browser cookie retry

Nếu chưa có cookie file, và `yt-dlp` fail với dấu hiệu bot-block như:

- `not a bot`
- `sign in to confirm`
- `the page needs to be reloaded`
- `confirm you're not a bot`

thì hệ thống retry **1 lần** với `cookiesfrombrowser`, nếu tìm thấy browser profile local.

Browser sources được dò tự động theo OS:

- macOS: Chrome / Chromium / Edge / Safari
- Windows: Chrome / Chromium / Edge / Firefox
- Linux: Chrome / Chromium / Firefox

Nếu retry vẫn fail, hệ thống map lỗi sang message thân thiện cho user.

## 7. Giới hạn hiện tại

Giới hạn hard-coded:

- `MAX_DURATION_SECONDS = 30 * 60`
- `MAX_FILE_BYTES = 500 * 1024 * 1024`

Tức là:

- tối đa **30 phút**
- tối đa **500MB**

Behavior:

- nếu external provider trả duration và duration vượt ngưỡng -> fail
- với `yt-dlp`, backend gọi `extract_info(download=False)` trước để check duration rồi mới download
- sau khi file đã tải về, nếu file size vượt ngưỡng -> fail

Message user-facing:

```text
Video quá dài hoặc quá nặng so với giới hạn hiện tại.
```

## 8. Chuẩn hóa file sau khi tải

Sau khi download xong, backend **không dùng file raw ngay**.

Nó chạy bước normalize để đảm bảo output cuối cùng là:

```text
input.mp4
```

### Mục tiêu của normalize

Đảm bảo video tương thích tốt cho bước xử lý tiếp theo và playback kiểu QuickTime.

### Logic

1. kiểm tra codec bằng `ffprobe`
2. nếu file chưa phù hợp thì transcode sang:
   - video: `h264`
   - audio: `aac`
   - pixel format: `yuv420p`
   - `+faststart`

Nếu file đã là MP4 tương thích sẵn thì chỉ rename/move về `input.mp4`.

## 9. Output contract

Service downloader trả về dict dạng:

```python
{
  "path": "/abs/path/to/input.mp4",
  "provider": "youtube",
  "title": "Video title",
  "duration": 123,
  "filesize": 4567890,
  "external_provider": "captapi",  # chỉ có khi dùng external
}
```

Field quan trọng:

- `path`: absolute path file đã sẵn sàng dùng
- `provider`: luôn là `"youtube"` cho flow này
- `external_provider`: provider external thực tế đã tải thành công

## 10. Error mapping

Flow hiện tại không expose raw exception ra user.

Một số mapping quan trọng:

### YouTube bot block

Nếu lỗi có dấu hiệu:

- `not a bot`
- `sign in to confirm`
- `the page needs to be reloaded`

thì user nhận:

```text
YouTube chặn tải từ server này. Vui lòng tải file video trực tiếp hoặc liên hệ admin cấu hình cookies.
```

### Generic failures

Fallback message:

```text
Tải video thất bại. Vui lòng thử lại hoặc tải file video trực tiếp.
```

### Restricted/private/login cases

Map thành lỗi thân thiện thay vì raw stack trace.

## 11. Job orchestration trong DrakonSub

Nếu engineer bên kia muốn clone behavior web hiện tại, flow job là:

### Endpoint

```text
POST /api/jobs/from-url
```

### Request body tối thiểu

```json
{
  "url": "https://www.youtube.com/watch?v=...",
  "selected_provider": "youtube"
}
```

### Response ban đầu

```json
{
  "job_id": "...",
  "source": "url",
  "provider": "youtube",
  "status": "downloading",
  "input_ready": false
}
```

### Background behavior

1. validate URL
2. tạo `job_id`
3. persist metadata job
4. background thread gọi `download_video_from_url(...)`
5. nếu success:
   - lưu `input.mp4`
   - job chuyển sang `downloaded`
6. nếu fail:
   - cleanup partials
   - job chuyển sang `error` / `failed`

### Endpoint lấy video gốc đã tải

```text
GET /api/jobs/{job_id}/input-video
```

## 12. Environment variables cần mang sang dự án khác

### External providers

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

### yt-dlp fallback

```text
YT_DLP_COOKIES_FILE
```

## 13. Pseudocode triển khai

```python
def download_video_from_url(url, output_dir, output_filename="input.mp4"):
    safe_url = validate_video_url(url)
    provider = detect_provider(safe_url)
    assert provider == "youtube"

    ensure_dir(output_dir)
    cleanup_partial_downloads(output_dir)

    for external_provider in available_external_providers_in_order():
        try:
            result = external_download(safe_url, output_dir, output_filename, external_provider)
            validate_duration_limit(result.duration)
            return finalize_downloaded_video(output_dir, result)
        except CreditsExhausted:
            cleanup_partial_downloads(output_dir)
            continue
        except ExternalDownloadError:
            cleanup_partial_downloads(output_dir)
            continue

    result = download_with_ytdlp(safe_url, output_dir, output_filename)
    return finalize_downloaded_video(output_dir, result)
```

## 14. Những điểm không nên bỏ qua khi clone flow

- Phải có **provider chain + credit-aware fallback**, không chỉ gọi một provider duy nhất.
- Phải có **yt-dlp fallback**, nếu không tỷ lệ fail thực tế sẽ cao.
- Phải có **cookie strategy** cho `yt-dlp`.
- Phải có **cleanup partial files** trước/sau fail.
- Phải có **normalize về MP4 tương thích** sau khi tải.
- Phải có **duration/file size limits** để tránh job nặng phá server.
- Phải map lỗi sang message thân thiện; không nên trả raw lỗi provider/yt-dlp cho user.

## 15. File code gốc để tham chiếu

- `auto_subtitle/url_import_service.py`
- `auto_subtitle/youtube_external_download.py`
- `auto_subtitle/web.py`
- `tests/test_url_import_service.py`
- `tests/test_url_import_web.py`
- `tests/test_youtube_external_download.py`

