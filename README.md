# OTTv2 Arena

Trang chủ `/` là **Đấu trường bot Python**. Sinh viên lập đội, viết bot, nộp kiểm tra, đấu xếp hạng hoặc thi giải do ban tổ chức mở. Ban tổ chức điều hành ở `/arena/admin`. `/arena/` vẫn dùng được cho các liên kết cũ. Bàn cờ chơi tay trước đây không còn được phục vụ trên website Arena.

## Luồng người chơi

1. Đăng ký bằng mã tham gia do ban tổ chức cấp. **Tạo đội** hoặc **vào đội** bằng mã 8 ký tự (tối đa 4 người).
2. Viết một file Python UTF-8 tối đa 64 KiB, định nghĩa `choose_move(observation)` và trả `{'from': [row, col], 'to': [row, col]}`. Xem [giao thức bot](arena/guide.html).
3. Nộp mã trên web. Runner kiểm tra 12 nửa lượt mẫu. Bản hợp lệ dùng được để đấu tập, xếp hạng hoặc đăng ký giải.
4. Ở **Chiến dịch boss**, leo tháp 5 boss mạnh dần (cần ≥ 1,5/2 điểm mỗi ải; 2/2 được ★★★) để lên bảng vàng và mở huy hiệu. Bấm tên đội bất kỳ để xem hồ sơ và mời giao hữu.
5. Ở **Đấu xếp hạng**, chọn bot và tìm đối thủ. Hệ thống ưu tiên đội chưa gặp trong 24 giờ, sau đó Elo gần nhất. Mỗi cặp có hai ván đổi màu; Elo cập nhật sau khi cả hai kết thúc.
6. Ở **Giải đấu**, chọn bot trước hạn. BTC khóa danh sách (bot không hợp lệ tự bị loại) rồi mở các vòng Swiss, vòng tròn hoặc loại trực tiếp. Bảng điểm có xếp loại Xuất sắc/Giỏi/Khá/Đạt.

Elo bắt đầu ở 1000; `round(K × (điểm ván / 2 − kỳ vọng))`, K=32, giảm khi gặp lại trong 24 giờ, tối thiểu 8. Elo **không** cộng vào bảng giải.

## Chạy tại máy phát triển

Yêu cầu Python 3.10+, Node.js 20+ và các package trong `arena_backend/requirements.txt`.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r arena_backend/requirements.txt
.\.venv\Scripts\python -m arena_backend admin admin
.\.venv\Scripts\python -m arena_backend serve
.\.venv\Scripts\python -m arena_backend worker --mode samples --slots 2
```

Mở `http://localhost:8080/`. Runner `samples` chỉ chạy **bot mẫu nguyên bản**. Runner production cần Docker và gVisor để chạy mã tùy ý của người chơi. Xem [triển khai và vận hành cho ~1.000 sinh viên](docs/ARENA_DEPLOY.md) và [Compose](deploy/README.md).

## Email xác minh

Tạo tài khoản cần email đã xác minh bằng mã 6 số gửi qua SMTP. Thêm vào file env của server (không commit mật khẩu):

```
ARENA_SMTP_HOST=smtp.gmail.com
ARENA_SMTP_PORT=587
ARENA_SMTP_USER=tai-khoan-gui@gmail.com
ARENA_SMTP_PASSWORD=mat-khau-ung-dung-16-ky-tu
ARENA_EMAIL_DOMAINS=vnu.edu.vn        # chỉ nhận email @vnu.edu.vn
ARENA_RATE_EMAIL_DAY=450              # dưới hạn mức ~500 email/ngày của Gmail cá nhân
```

Bật hoặc tắt "Bắt buộc xác minh email" trong trang quản trị. Sinh viên quên mật khẩu tự đặt lại bằng mã gửi về email. Chi tiết ở [docs/ARENA_DEPLOY.md](docs/ARENA_DEPLOY.md).

## Kiểm thử

```powershell
npm test
python -m unittest discover -s arena_backend/tests -v
node scripts/arena-smoke.mjs
python scripts/arena-load.py --base http://127.0.0.1:8080 --users 1000   # chỉ trên dữ liệu staging
```
