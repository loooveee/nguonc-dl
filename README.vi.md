# nguonc-dl

[English](README.md) · **Tiếng Việt**

> [!WARNING]
> **Dự án chỉ phục vụ mục đích học tập.** Dự án cho thấy cách HLS streaming, giải mã luồng phát và
> tải song song hoạt động. Không dùng để đăng lại, chia sẻ hay bán phim.

Tải phim lẻ và phim bộ từ **phim.nguonc.com** về máy dưới dạng file **MP4**.

- Dán link là tải: phim lẻ ra một file, phim bộ tải **toàn bộ các tập**
- Xem **dung lượng từng chất lượng** trước khi tải (`--info`)
- Tải nhanh bằng **aria2c** (không có thì dùng yt-dlp)
- Tự bỏ **đoạn quảng cáo** chèn giữa phim
- Thanh tiến độ trực tiếp: mỗi tập một thanh, thêm một dòng tổng, có tốc độ và thời gian còn lại
- Tắt giữa chừng thì chạy lại đúng lệnh cũ để tải tiếp

## Cài đặt

1. Cài [Python 3.10 trở lên](https://www.python.org/downloads/)
   (trên Windows, tích ô **Add python.exe to PATH** ở màn hình đầu tiên của bộ cài)
2. Tải dự án này: **Code** → **Download ZIP**, rồi giải nén
3. Mở cửa sổ lệnh trong thư mục vừa giải nén (Windows: bấm vào thanh địa chỉ của thư mục,
   gõ `cmd` rồi Enter), sau đó chạy:
   ```bash
   python -m pip install -r requirements.txt
   ```
4. Đặt `aria2c.exe` cạnh file `nguonc-dl.py` (nên làm, nhanh hơn nhiều lần)
   - aria2c: https://github.com/aria2/aria2/releases (file zip `win-64bit`, lấy `aria2c.exe` ra)
   - ffmpeg đã được cài ở bước 3. Bạn cũng có thể đặt `ffmpeg.exe` của mình cạnh script.

## Cách dùng

```bash
python nguonc-dl.py "https://phim.nguonc.com/phim/ten-phim"
```

| Lệnh | Tác dụng |
|---|---|
| `python nguonc-dl.py <link>` | Tải phim lẻ, hoặc toàn bộ tập của phim bộ |
| `python nguonc-dl.py ten-phim` | Chỉ cần phần tên trong link cũng được |
| `python nguonc-dl.py <link> --info` | Chỉ xem dung lượng từng chất lượng |
| `python nguonc-dl.py <link> -e 1-5,8` | Chỉ tải tập 1 đến 5 và tập 8 (`10-` = từ tập 10 tới hết) |
| `python nguonc-dl.py <link> -q 720` | Chọn chất lượng (`best`, `worst`, `1080`, `720`, ...) |
| `python nguonc-dl.py <link> -s 2` | Dùng server 2 (vd thuyết minh thay vì vietsub) |
| `python nguonc-dl.py <link> --list` | Chỉ liệt kê server và tập |
| `python nguonc-dl.py <link> -o D:\Phim` | Lưu vào thư mục khác |
| `python nguonc-dl.py <link> -j 3` | Tải 3 tập cùng lúc (mặc định 2) |
| `python nguonc-dl.py <link> --lang vi` | Hiện thông báo bằng tiếng Việt |

File được lưu trong `downloads/<tên phim>/`, đặt tên kiểu `Ten.Phim.S01E01.mp4`.

Thông báo mặc định bằng **tiếng Anh**, máy cài tiếng Việt thì tự chuyển sang tiếng Việt.
Dùng `--lang vi` hoặc `--lang en` để tự chọn. Dùng `--no-color` (hoặc đặt biến `NO_COLOR=1`) để in chữ trơn.

Chạy `python nguonc-dl.py -h` để xem tất cả tuỳ chọn.

## Tập không tải được

Tập nào lỗi sẽ hiện màu đỏ kèm lý do, và được ghi vào **`failed.txt`** trong thư mục phim (tên tập,
link và lý do). Chạy lại đúng lệnh cũ để thử lại: tập đã xong được bỏ qua, tập tải dở được tải tiếp.
`failed.txt` tự xoá khi không còn tập nào lỗi.

## Gặp lỗi?

- **`python` is not recognized:** cài lại Python và tích ô **Add python.exe to PATH**
- **"Thiếu thư viện":** chạy lại `python -m pip install -r requirements.txt`
- **"bị máy chủ giới hạn lượt":** trang chỉ cho khoảng 20 link mỗi giờ. Đợi một lúc rồi chạy lại đúng
  lệnh cũ, hoặc tải ít tập hơn mỗi lần bằng `-e`
- **"Cần HTTP/2" / HTTP 403:** chạy `python -m pip install "httpx[http2]"`
- **Tải chậm:** đặt `aria2c.exe` cạnh `nguonc-dl.py`
- **Ctrl+C:** dừng ngay. Chạy lại đúng lệnh cũ để tải tiếp

## Đóng góp

Rất hoan nghênh báo lỗi và pull request. Thay đổi nhằm vượt captcha hay các lớp bảo vệ khác sẽ
không được nhận.

- **Báo lỗi:** mở một [issue](https://github.com/loooveee/nguonc-dl/issues), ghi lệnh đã chạy, nội dung
  lỗi (hoặc `failed.txt`), hệ điều hành và `python --version`.
- **Gửi bản sửa:** fork repo, tạo nhánh, commit, push lên fork của bạn rồi mở pull request.
  Ghi rõ đã sửa gì, vì sao, và đã thử thế nào.
- **Quy tắc:**
  - Giữ toàn bộ code trong `nguonc-dl.py`. Docstring ở đầu file có sơ đồ code.
  - Mọi thông báo viết đủ hai thứ tiếng: `t("English", "Tiếng Việt")`.
  - Dùng các hàm màu (`green()`, `red()`...) thay vì mã ANSI trực tiếp.

## Miễn trừ trách nhiệm

**Dự án chỉ phục vụ mục đích học tập.** Bạn tự chịu trách nhiệm với cách mình sử dụng.
Hãy tôn trọng bản quyền và điều khoản sử dụng của trang web.

Dự án không liên quan tới phim.nguonc.com. Tác giả không chịu trách nhiệm cho việc sử dụng sai mục đích.

## Giấy phép

MIT © loooveee
