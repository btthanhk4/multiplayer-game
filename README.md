# OTTv2 Arena

Trang chủ `/` là **Đấu trường bot Python**. Người chơi viết bot, nộp kiểm tra, đấu xếp hạng tự động hoặc đăng ký giải do ban tổ chức mở. `/arena/` vẫn dùng được cho các liên kết cũ. Bàn cờ chơi tay trước đây không còn được phục vụ trên website Arena.

## Luồng người chơi

1. Đăng ký bằng mã tham gia do ban tổ chức cấp và đăng nhập.
2. Viết một file Python UTF-8 tối đa 64 KiB, định nghĩa `choose_move(observation)` và trả `{'from': [row, col], 'to': [row, col]}`. Xem [giao thức bot](arena/guide.html).
3. Nộp mã. Runner kiểm tra tối đa 12 nửa lượt mẫu. Bản hợp lệ có thể dùng để đấu tập, xếp hạng hoặc đăng ký giải.
4. Ở **Đấu xếp hạng**, chọn bot hợp lệ và tìm đối thủ. Hệ thống ưu tiên người chưa gặp trong 24 giờ, sau đó Elo gần nhất. Nếu chưa có ai thì bot ở hàng chờ; có thể hủy trước khi ghép.
5. Mỗi cặp có hai ván đổi màu. Sau khi cả hai kết thúc, Elo cập nhật một lần; người xem có thể mở từng ván và replay.

Elo bắt đầu ở 1000. Mỗi ván thắng/hòa/thua được 1/0,5/0 điểm; điểm cặp dùng tổng hai ván. Công thức Elo: `round(K × (điểm ván / 2 − kỳ vọng))`, K=32 và giảm cho các cặp gặp lại trong 24 giờ, tối thiểu 8. Điểm Elo **không** cộng vào bảng giải chính thức.

Trong giải, ban tổ chức khóa bot sau hạn rồi mở từng vòng. Swiss ghép gần điểm và hoán đổi cặp để giảm tái đấu; vòng tròn cho mỗi cặp gặp một lần. Mỗi cặp thi hai ván đổi màu; thắng/hòa/thua cặp được 1/0,5/0 điểm. Đồng điểm xét Buchholz, sau đó điểm ván.

## Chạy tại máy phát triển

Yêu cầu Python 3.10+, Node.js 20+ và các package trong `arena_backend/requirements.txt`.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r arena_backend/requirements.txt
.\.venv\Scripts\python -m arena_backend admin admin
.\.venv\Scripts\python -m arena_backend serve
```

Mở `http://localhost:8080/`. Ở cửa sổ khác, có thể chạy runner chỉ dành cho **bot mẫu nguyên bản**:

```powershell
.\.venv\Scripts\python -m arena_backend worker --mode samples
```

Runner production cần Docker và gVisor để chạy mã tùy ý của người chơi. Xem [triển khai](docs/ARENA_DEPLOY.md) và [Compose](deploy/README.md).

## Kiểm thử

```powershell
npm test
python -m unittest discover -s arena_backend/tests -v
node scripts/arena-smoke.mjs
```

Các test chức năng không phải phép thử tải 5.000 người dùng. Trước khi mở giải lớn cần thử tải API, SQLite và nhiều runner với bộ bot đại diện.
