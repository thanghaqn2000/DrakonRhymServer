# Hướng dẫn tích hợp Extension ↔ DrakonRhymServer

Tài liệu cho team Extension về cách redirect user sang web để tải audio đã pitch-shift.

---

## 1. URL redirect

Khi user nhấn nút **Download** trong extension, redirect (hoặc `chrome.tabs.create`) sang:

```
https://<host>/download?url=<youtube_url>&pitch=<pitch_value>
```

Chỉ cần **2 query param** ở dưới đây. Tất cả thông tin còn lại (title, kênh, thumbnail, duration, views) **web tự fetch** qua YouTube — extension không cần gửi.

### Query params

| Tên     | Bắt buộc | Kiểu  | Range / Format                             | Mô tả                                              |
|---------|----------|-------|--------------------------------------------|----------------------------------------------------|
| `url`   | ✅       | string | URL YouTube đầy đủ (đã URL-encode)        | Chỉ chấp nhận domain `youtube.com` / `m.youtube.com` / `music.youtube.com` / `www.youtube.com` / `youtu.be`. |
| `pitch` | ✅       | float | `-6.0` đến `6.0`, step `0.1`               | Giá trị slider hiện tại trong extension. Web sẽ round về step 0.1 và snap với `ROUND_HALF_UP`. |

### Ví dụ

```
https://example.com/download
  ?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ
  &pitch=2.5
```

→ Tải audio của video, dịch lên 2.5 nửa cung.

`pitch=0` cũng OK (tải nguyên gốc, không shift).

> Web yêu cầu user **đăng nhập Google** trước khi tải. Mỗi user có quota mặc định **20 download/ngày**. Extension không cần làm gì — web tự xử lý sign-in qua Google Identity Services. Lần đầu user đến web sẽ thấy modal "Sign in to download"; sau khi sign in OK, ID token cache trong `localStorage` và sống ~1 giờ (theo `exp` claim), trong khoảng đó user không phải đăng nhập lại.

---

## 2. Behavior của web sau redirect

1. Hiển thị **placeholder** ngay lập tức (title "Loading…", thumbnail xám).
2. Gọi `GET /api/metadata?url=<url>` để lấy title / channel / duration / thumbnail / view count từ YouTube. UI fill dần khi metadata về.
3. Song song, gọi `GET /api/download?url=<url>&pitch=<pitch>`:
   - **Phase 1 (server đang xử lý)**: ring chạy 0 → 75% trong khoảng `videoDuration × 0.18` giây (min 6s, max 90s). Step labels: Connecting → Streaming → Extracting → Wrapping.
   - **Phase 2 (server bắt đầu stream MP3)**: ring chạy 75 → 100% theo bytes thật từ `Content-Length` header.
4. Khi xong: hiện trạng thái "Ready to save". User nhấn **Download** → browser save file MP3 từ Blob trong memory (không re-fetch server).

### Filename file tải về

Format: `<video_title> - pitch = <pitch>.mp3`

Ví dụ: `Rick Astley Never Gonna Give You Up Official Video 4K Remaster - pitch = 2.5.mp3`

`video_title` được sanitize ở client (loại bỏ các ký tự `< > : " / \ | ? *` cấm trên filesystem, collapse khoảng trắng).

---

## 3. Error cases mà web sẽ hiển thị

Extension **không cần làm gì** — web tự render trạng thái lỗi và có nút **Restart**:

| Tình huống                                | UI thể hiện                                                   |
|-------------------------------------------|----------------------------------------------------------------|
| Thiếu `url`                               | "Missing URL." + hướng dẫn quay lại extension                  |
| `url` không phải YouTube                  | "Invalid URL — only YouTube domains are accepted."             |
| `pitch` ngoài `[-6, 6]`                   | 422 → "Input should be less than or equal to 6" (tương tự với min) |
| Video private / removed / region-locked   | "Failed to download audio from the given URL."                 |
| Network drop giữa chừng                   | "Stream interrupted." + nút Restart                            |
| Server timeout (yt-dlp/ffmpeg quá lâu)    | 504 → web hiện message                                          |

---

## 4. Lưu ý kỹ thuật

- **CORS đang mở** (`*`) trong dev. Production sẽ khóa qua env `DRAKON_ALLOWED_ORIGINS` — báo trước host extension để add vào whitelist.
- **Server không nhận title từ extension**. Lý do: title từ YouTube luôn là canonical, tránh việc extension/web mismatch khi YouTube đổi title.
- **Concurrency cap = 2** trên server. Nếu nhiều user nhấn cùng lúc, request thứ 3 trở đi sẽ queue (vẫn 200 OK, chỉ chậm hơn).
- **Chỉ output MP3** (192 kbps VBR, libmp3lame). Các format M4A/WAV/FLAC trên UI đang disable, để dành cho sau.

---

## 5. Tóm tắt cho người chỉ cần 1 dòng

> Gửi user tới `/download?url=<youtube>&pitch=<float>`. Hết. Web lo phần còn lại.
