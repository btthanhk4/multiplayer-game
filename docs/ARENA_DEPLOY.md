# OTTv2 Arena — triển khai trên một máy chủ

## Có gì trong V1

- Game người chơi hiện tại + đấu trường tại `/arena/`, cùng web server.
- Đăng ký bằng mã mời khi public, mật khẩu scrypt, phiên đăng nhập cookie HttpOnly, CSRF, phân quyền người chơi/BTC. Không lưu token vào localStorage.
- File Python một file, tối đa 64 KiB; nhiều phiên bản bất biến; kiểm tra qua hàng đợi; chỉ chủ bài được đọc source qua API.
- Trận tập hai ván đổi màu; vòng tròn (2–32 bot), Swiss theo điểm (2–500 bot); khóa bài trước khi chạy; bảng điểm tính lại từ kết quả.
- Khán đài tối đa bốn bàn, replay đầy đủ từng nước và nhật ký điều hành.
- SQLite WAL lưu metadata, source riêng tư, replay và job. V1 không cần Workers/D1/R2. Một máy, một runner trước; không đặt database trên ổ mạng.

**Mức sẵn sàng:** bản V1 để thử nội bộ và chuẩn bị server. Chưa chứng nhận an toàn production hay tải 500 bot. Cần chạy smoke gVisor, rà soát sandbox và tổng duyệt trên máy thật trước khi nhận code công khai.

## 1. Chạy thử trên Windows

Yêu cầu Python 3.10+, Node.js 20+; Node dùng `rules.js` hiện có để xử lý luật. Python không viết lại luật.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r arena_backend/requirements.txt
.\.venv\Scripts\python -m arena_backend admin admin
.\.venv\Scripts\python -m arena_backend serve
```

Mật khẩu admin được hỏi trực tiếp, không ghi vào command line. Mở **http://localhost:8080/arena/**. Không dùng `127.0.0.1` thay cho localhost nếu giữ cấu hình Origin mặc định.

Terminal thứ hai, nếu chưa có Docker/gVisor:

```powershell
.\.venv\Scripts\python -m arena_backend worker --mode samples
```

Chế độ `samples` chỉ thực thi source trùng hash với ba file trong `sdk/examples/`. Trong Xưởng bot, bấm bot mẫu rồi nộp nguyên bản. Mã thay đổi sẽ không được chạy trên host; job báo cần runner Docker. UI/replay ghi rõ thử nội bộ. Không public server với chế độ này.

Để thử code tùy chỉnh do chính BTC viết trên Docker Desktop:

```powershell
docker build -t ottv2-bot:1 arena_backend/sandbox
.\.venv\Scripts\python scripts/arena-sandbox-smoke.py --mode docker-dev
.\.venv\Scripts\python -m arena_backend worker --mode docker-dev
```

`docker-dev` sử dụng runtime Docker mặc định, chỉ dùng cho phát triển cục bộ với code tin cậy. Runner CLI từ chối chế độ này khi ARENA_ORIGIN là địa chỉ public. Không chạy đồng thời runner `samples` và `docker-dev` trên cùng hàng đợi.

## 2. Hợp đồng bot và thể lệ V1

Xem `/arena/guide.html` hoặc [sổ tay](../arena/guide.html). Bot export hàm `choose_move(observation)` trả `{from: [r,c], to: [r,c]}`. Python khởi tạo mới mỗi lượt, random seeded; không giữ trạng thái chạy nền. Runtime trong image: Python 3.12, thư viện chuẩn.

Giới hạn: 1 giây CPU/process, 3 giây wall sau handshake, 256 MiB RAM, 4 KiB JSON, 4 KiB stderr/lượt. RLIMIT_NPROC hard=1 sau khởi động adapter, network none, cap-drop ALL, read-only rootfs, no-new-privileges. Giới hạn PID/cgroup ngoài là 64 để gVisor khởi động các tiến trình phụ trợ; kiểm thử child-process xác nhận bot vẫn không tạo được tiến trình con. Source/observation truyền qua stdin, không mount host vào sandbox; không đưa environment secret vào container. Docker daemon/socket chỉ runner sử dụng, không cấp cho API hoặc bot.

Không có nước hợp lệ được tính hòa theo **bổ sung thể lệ Arena V1**; engine gốc không thay đổi. Bot lỗi là thua kỹ thuật; lỗi hạ tầng là job lỗi và BTC thử lại, không tự xử thua. Chẩn đoán lỗi bot trong trận được giữ ở audit BTC, không xuất traceback riêng tư trong replay công khai.

V1 chỉ đăng ký bản **đã valid trước deadline**. Không có cơ chế grace cho bản đang kiểm tra; giao diện và thể lệ ghi rõ để thí sinh nộp sớm. Không khóa sớm trước hạn. Snapshot cố định submission/hash. Chưa hỗ trợ rút thí sinh sau khóa, xử lý phúc khảo sửa điểm hay tự tổ chức chung kết. BTC có thể tạo giải tiếp theo riêng.

Swiss là thuật toán xác định, ghép gần điểm và ưu tiên tránh gặp lại; có thể gặp lại khi ghép tham lam không tìm thấy đối thủ mới. Không tuyên bố tương thích chuẩn FIDE. BYE ưu tiên ít BYE rồi thấp điểm, cho 1 điểm cặp, 0 điểm ván, 0 đối thủ Buchholz. Xếp điểm cặp → Buchholz → điểm ván; còn bằng thì đồng hạng. Kết quả của một cặp chỉ được tính khi đủ cả hai ván.

## 3. Chuẩn bị Linux server

Khuyến nghị bắt đầu 4 lõi, 16 GB RAM, SSD trống 50 GB; một runner xử lý một ván tại một thời điểm. Nhiều tài khoản không đồng nghĩa nhiều ván chạy đồng thời. Đo tải trước khi tăng worker hoặc số suất.

1. Cài Git, Python 3.12 + venv, Node.js LTS, Docker Engine và **gVisor (`runsc`)**. Làm theo [hướng dẫn chính thức gVisor](https://gvisor.dev/docs/user_guide/quick_start/docker/). Không tự hạ runtime khi `runsc` thiếu.
2. Clone repo vào `/opt/ottv2`, checkout đúng commit phát hành. Tạo `.venv`, cài requirements. Mã nguồn và venv do tài khoản triển khai quản lý; hai tài khoản dịch vụ không được sửa code.
3. Tạo group `ottv2`, hai user hệ thống `ottv2-web`, `ottv2-runner`; cả hai thuộc group `ottv2`, chỉ runner thuộc group `docker`. Thành viên nhóm Docker có quyền mạnh trên host; không dùng tài khoản API cho runner.
4. Tạo `/var/lib/ottv2`, sở hữu group `ottv2`, quyền `2770`, hai service ghi được. Chỉ mục này chứa DB/WAL/source; không đặt vào thư mục web hay repo public.
5. Sao chép `deploy/.env.example` ra `/etc/ottv2.env`, thay Origin và mã mời ngẫu nhiên. EnvironmentFile không phải file được app tự load: systemd sẽ truyền biến; khi chạy CLI thủ công phải export cùng biến tương ứng. Giữ file bí mật, không commit.
6. Build image, chạy kiểm tra sandbox dưới tài khoản runner:

```sh
docker build -t ottv2-bot:1 /opt/ottv2/arena_backend/sandbox
/opt/ottv2/.venv/bin/python /opt/ottv2/scripts/arena-sandbox-smoke.py --mode docker
```

7. Tạo admin tại đúng thư mục data (chạy bằng tài khoản dịch vụ có quyền ghi, umask 0007):

```sh
cd /opt/ottv2
umask 0007
.venv/bin/python -m arena_backend --data /var/lib/ottv2 admin admin
```

8. Cài hai unit trong `deploy/` vào systemd sau khi đã tạo user/group/path. Đọc và điều chỉnh paths trước khi enable. API bind `127.0.0.1:8080`; runner tự đọc cùng SQLite.
9. Bật `ottv2-api` và `ottv2-runner`, kiểm tra journal và `/api/health`. API vẫn nhận bài khi runner tắt, nhưng API dừng thì không nhận bài được.

V1 tạo một container mới cho mỗi nước đi, ưu tiên cách ly dễ kiểm tra. Chi phí khởi động có thể lớn; chưa cam kết chạy 4.740 ván trong thời gian nào. Image ID được ghi vào replay. Trước giải cần pin image, engine và phiên bản triển khai, không rebuild/đổi code giữa vòng.

## 4. Public qua Cloudflare Tunnel

Tạo named Tunnel và hostname ổn định trên tên miền của BTC. Trỏ service về `http://127.0.0.1:8080`; có cấu hình mẫu tại `deploy/cloudflared.example.yml`. Không public cổng Docker, DB hoặc runner. Xem [tài liệu Cloudflare Tunnel](https://developers.cloudflare.com/tunnel/).

Đặt `ARENA_ORIGIN=https://arena.ten-mien-cua-ban` đúng URL trình duyệt dùng. Khi đó cookie Secure được bật, mọi POST khác Origin bị từ chối. `ARENA_REGISTRATION_CODE` bắt buộc khi public. V1 chỉ phục vụ cùng origin, không mở CORS tùy ý. Nếu muốn giữ riêng UI Arena trên GitHub Pages, cần triển khai xác thực/CORS riêng trước; game người chơi cũ vẫn có thể ở Pages.

`ARENA_TRUST_CLOUDFLARE=1` chỉ dùng khi API bind loopback, cloudflared là lối vào tin cậy. Khi đó IP từ CF-Connecting-IP dùng để giới hạn đăng ký/login; không bật nếu reverse proxy không kiểm soát được header. Chưa có OAuth/email reset ở V1; `admin USER` có thể đặt lại mật khẩu và cấp quyền admin, vì vậy chỉ BTC được truy cập CLI này. Khôi phục tài khoản người chơi qua DB cần quy trình xác minh thủ công, chưa có UI reset.

Quick Tunnel chỉ để thử, không dùng làm địa chỉ giải chính thức. Khi bạn cung cấp server/domain, mới thực hiện các bước cài và public thực tế.

## 5. Sao lưu, khôi phục, sự cố

```sh
ARENA_DATA=/var/lib/ottv2 .venv/bin/python -m arena_backend backup /duong-dan-backup/arena-2026-10-16.sqlite3
```

Lệnh dùng SQLite backup API để tạo bản nhất quán gồm tài khoản, source, snapshot, kết quả và replay. Không copy riêng file DB đang mở và bỏ WAL. Không ghi đè backup đã có. Bảo vệ bản backup như source riêng tư; giữ bản ngoài máy runner.

Khôi phục: dừng cả API và runner, giữ nguyên data cũ để đối chiếu, khôi phục bản backup thành `arena.sqlite3` trong một thư mục mới, đặt quyền group thích hợp, đổi ARENA_DATA/ReadWritePaths rồi khởi động. Không đặt DB khôi phục cạnh WAL/SHM cũ. Kiểm tra integrity, số entries và kết quả trước khi mở lại.

Job có lease 90 giây, heartbeat 15 giây, attempt token để chặn kết quả cũ. Job bị mất runner được nhận lại tối đa ba lần tự động; lỗi hạ tầng thông thường chuyển `failed`, BTC retry có audit. Worker quét container mang label riêng của database mỗi 30 giây, xóa container không còn lease hợp lệ sau sự cố. Trận lỗi chạy lại từ đầu; không cam kết code dùng nguồn ngẫu nhiên riêng/thời gian sẽ ra cùng ván. Replay đã ghi được tái hiện từ log nước đi.

Hạn mức mặc định: 10 bài nộp/giờ/tài khoản, 10 cặp tập/giờ, mỗi người tối đa một cặp tập đang chờ/chạy. Danh sách UI hiện 100 bài/trận/job mới nhất; chưa có tìm kiếm kho lịch sử lớn. Theo dõi dung lượng DB, lượt chờ, thời gian ván, OOM và log. Sao lưu mỗi ngày và trước/sau khóa giải. V1 chưa tự xóa dữ liệu theo retention; BTC phải chốt chính sách trước public.

## 6. Kiểm thử và điều kiện mở giải

```sh
npm ci
npm test
.venv/bin/python -m unittest discover -s arena_backend/tests -v
.venv/bin/python scripts/arena-sandbox-smoke.py --mode docker
```

Browser smoke: xem đầu file `scripts/arena-smoke.mjs`. Chạy qua môi trường data thử trong `artifacts/`, không dùng DB giải đang diễn ra.

Trước public: smoke gVisor đạt; rà soát sandbox/permissions; thử mất runner, backup/restore; chốt thể lệ; chạy hết một giải kín 16–32 bot. Trước 500 suất: tổng duyệt toàn bộ workload, nguồn điện/mạng, số worker, công bằng CPU, thời gian vòng và hạn mức dịch vụ. Các kiểm thử phát triển không thay cho thử tải hoặc đánh giá bảo mật độc lập.
