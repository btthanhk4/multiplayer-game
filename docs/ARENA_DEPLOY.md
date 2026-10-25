# OTTv2 Arena — triển khai và vận hành cho ~1.000 tân sinh viên

## Có gì trong V2

- **Tài khoản và đội**: sinh viên tự đăng ký bằng mã tham gia; tạo đội hoặc vào đội bằng mã 8 ký tự (mặc định tối đa 4 người, `ARENA_TEAM_SIZE`). Bot, Elo và đăng ký giải thuộc về đội.
- **Dashboard sinh viên** (`/`): lộ trình 5 bước, thẻ đội/bot/Elo/giải đang mở, vị trí trong hàng chờ, bảng điểm có tìm kiếm, xem từng vòng, khán đài 4 bàn kèm replay.
- **Dashboard ban tổ chức** (`/arena/admin`): số liệu hệ thống và ước tính thời gian chấm, công tắc sự kiện (đăng ký, nộp bài, xếp hạng, đấu tập, chế độ chỉ xem), thông báo toàn trang, quản lý giải, nhập Google Form, tìm và xử lý đội/tài khoản, xem mã nghi sao chép, hàng đợi, nhật ký.
- **Thể thức**: Swiss (≤ 2.000 đội), vòng tròn (≤ 32), loại trực tiếp (≤ 1.024, có thể lấy top N của giải trước). Có tự mở vòng, truất quyền giữa giải (xử thua các ván chưa đấu) và xếp loại Xuất sắc/Giỏi/Khá/Đạt. Bảng điểm xuất ra CSV.
- **Máy chấm**: mỗi bot một sandbox cho cả ván (giao thức 2). Có nhiều luồng chấm song song (`--slots`), hàng đợi ưu tiên (kiểm tra bot → giải → xếp hạng → đấu tập), tự chạy lại lỗi hạ tầng tối đa 3 lần, và trả job về hàng đợi khi runner dừng.
- **Tạo động lực (V3)**: chiến dịch 5 boss mạnh dần (bot trong `sdk/examples` và `sdk/bosses`, tự đăng ký và được kiểm tra khi API khởi động), bảng vàng chiến dịch, 17 huy hiệu, bảng tin trực tiếp (lật kèo, thắng trắng, hạ boss, vô địch), hồ sơ công khai của đội kèm đồ thị Elo, giao hữu với đội bất kỳ, bục trao giải, replay có link chia sẻ (`/#watch=<id>`), tua nhanh và phóng to, cùng pháo hoa khi đội thắng.
- SQLite WAL, kết nối dùng lại theo luồng, cache 2–3 giây cho dữ liệu công khai, và giới hạn tần suất phù hợp với việc cả trường dùng chung một IP NAT.

## 1. Chạy thử trên máy phát triển

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r arena_backend/requirements.txt
.\.venv\Scripts\python -m arena_backend admin admin
.\.venv\Scripts\python -m arena_backend serve
.\.venv\Scripts\python -m arena_backend worker --mode samples --slots 2   # cửa sổ khác
```

Mở `http://localhost:8080/`. Đăng nhập bằng tài khoản admin rồi vào `/arena/admin`. Chế độ `samples` chỉ chạy **nguyên bản** ba bot trong `sdk/examples/` và các boss trong `sdk/bosses/`; mã khác sẽ báo cần runner Docker. Chế độ `docker-dev` dùng runtime Docker mặc định, chỉ dành cho mã tin cậy khi phát triển. Không public server khi đang dùng hai chế độ này; CLI cũng từ chối chúng khi `ARENA_ORIGIN` là địa chỉ public.

## 2. Ước tính công suất

Kết quả `scripts/arena-load.py` trên một laptop Windows, khi client và server chạy chung một máy:

| Tình huống | Kết quả |
|---|---|
| 1.000 lượt đăng ký (64 đồng thời) | 26 giây, 0 lỗi. Chi phí chính là băm mật khẩu scrypt. |
| 1.000 dashboard mở, mỗi tab refresh ~15 giây | ~330 req/s, p95 < 20 ms, 0 lỗi |
| Vòng Swiss 1.000 đội: ghép cặp + bảng điểm | < 2 giây |

**Máy chấm là nút cổ chai thật sự.** Mỗi ván cần 2 sandbox gVisor (khởi động ~1–2 giây) cộng thời gian bot suy nghĩ. Một ván thường mất 10–30 giây, tối đa 2 × 60 giây suy nghĩ cộng chi phí khởi động. Một vòng Swiss 1.000 đội có 1.000 ván:

| Luồng chấm | Ván trung bình 20 s | Ván trung bình 40 s |
|---|---|---|
| 8 (`--slots 8`, máy 8–12 lõi) | ~42 phút/vòng | ~83 phút/vòng |
| 16 (2 máy runner × 8) | ~21 phút/vòng | ~42 phút/vòng |

Chiến dịch boss và giao hữu cũng tốn máy chấm: mỗi lượt là 2 ván, ưu tiên ngang đấu tập (sau giải và xếp hạng). Mỗi đội chỉ có một lượt boss và một lượt giao hữu chạy cùng lúc, và có giới hạn 30 lượt boss/giờ (`ARENA_RATE_CAMPAIGN_TEAM`). Nếu 1.000 đội cùng leo tháp, hàng đợi có thể dài vài giờ; khi đó hãy tắt *Mở chiến dịch boss* hoặc tăng `--slots`. Boss 5 nghĩ tới ~0,6 giây mỗi nước, nên ván với boss 5 dài hơn ván thường.

Khuyến nghị: dùng máy API 4 lõi / 8 GB. Mỗi máy runner có bao nhiêu lõi trống thì đặt `--slots` bằng chừng đó (mỗi sandbox `--cpus=1`, `--memory=256m`, nên 8 slot cần khoảng 4 GB RAM cho bot). Nhiều runner có thể dùng chung một database **trên cùng máy**. Không đặt SQLite lên ổ mạng; nếu cần runner ở máy khác, phải thiết kế lại (ví dụ chuyển sang PostgreSQL). Admin dashboard hiển thị "Ước tính xong" dựa trên thời gian chấm thực tế của giờ gần nhất.

Trước sự kiện, chạy thử tải trên **máy staging giống hệt production**:

```sh
python scripts/arena-load.py --base https://staging.example.org --users 1000 --invite MÃ --unique-bots
```

## 3. Hợp đồng bot và thể lệ

Xem `/arena/guide.html`. Tóm tắt: `choose_move(observation)` trả `{from: [r,c], to: [r,c]}`. Mỗi bot được nạp một lần mỗi ván và giữ biến toàn cục trong ván. Giới hạn: 3 giây/lượt, quỹ 60 giây suy nghĩ mỗi ván, 5 giây nạp mã, 256 MiB RAM, 4 KiB kết quả, 1 MiB log, 400 nửa lượt (đủ thì hòa). Sandbox chặn mạng, đặt rootfs chỉ đọc, `cap-drop ALL`, `no-new-privileges`, `RLIMIT_NPROC=1` sau khi adapter khởi động, giới hạn PID cgroup 64 và CPU tích lũy bằng quỹ thời gian + 20 giây. `print()` của bot được chuyển sang stderr nên không thể làm hỏng giao thức.

Bot lỗi thua kỹ thuật ván đó; nếu cả hai bot cùng lỗi thì ván hòa. Lỗi hạ tầng không bao giờ bị tính là bot thua: hệ thống tự chạy lại 3 lần, sau đó job chờ BTC bấm "Thử lại". Chẩn đoán lỗi bot trong trận chỉ nằm trong nhật ký BTC. Riêng log kiểm tra bot thì đội tự xem được, để biết cách sửa.

Thay đổi `rules.js` hoặc `LIMITS` sẽ làm các giải đã khóa từ chối mở vòng mới. Chốt engine trước khi khóa danh sách giải đầu tiên.

## 4. Chuẩn bị Linux server

1. Cài Git, Python 3.12 + venv, Node.js LTS, Docker Engine và **gVisor (`runsc`)** theo [hướng dẫn gVisor](https://gvisor.dev/docs/user_guide/quick_start/docker/). Nếu thiếu `runsc`, không tự hạ xuống runtime mặc định.
2. Clone vào `/opt/ottv2`, checkout commit phát hành, tạo `.venv`, cài requirements.
3. Tạo group `ottv2` và hai user hệ thống `ottv2-web`, `ottv2-runner`. Chỉ runner thuộc group `docker`.
4. Tạo `/var/lib/ottv2` (group `ottv2`, quyền `2770`).
5. Sao chép `deploy/.env.example` ra `/etc/ottv2.env`. Đặt Origin, mã mời ngẫu nhiên và `ARENA_RUNNER_SLOTS`. Giữ file bí mật.
6. `docker build -t ottv2-bot:1 /opt/ottv2/arena_backend/sandbox`, rồi chạy `scripts/arena-sandbox-smoke.py --mode docker` dưới tài khoản runner. Mọi dòng phải là PASS.
7. Tạo admin: `.venv/bin/python -m arena_backend --data /var/lib/ottv2 admin admin` (umask 0007).
8. Cài hai unit trong `deploy/`, bật lên, rồi kiểm tra `/api/health` và tab Tổng quan của admin (phải thấy "N luồng chấm").

Docker Compose: xem [deploy/README.md](../deploy/README.md).

## 5. Public qua Cloudflare Tunnel

Trỏ hostname về `http://127.0.0.1:8080`. Đặt `ARENA_ORIGIN=https://…` đúng với URL trình duyệt dùng, và bật `ARENA_TRUST_CLOUDFLARE=1` khi API chỉ bind loopback, để giới hạn tần suất dùng IP thật. Mã mời (`ARENA_REGISTRATION_CODE`) là bắt buộc khi public. Đây là lớp chặn chính, vì giới hạn theo IP được đặt rộng (mặc định 3.000 lượt đăng ký/giờ/IP) cho cả trường chung NAT. Giới hạn theo tài khoản/đội vẫn chặt: 20 lần đăng nhập sai/15 phút/tài khoản, 20 bài nộp/giờ/đội, tối đa 3 bài chờ kiểm tra/đội. Tất cả đều chỉnh được bằng biến `ARENA_RATE_*`.

## 6. Thu bài bằng Google Forms

Thiết kế form:

1. **Mã đội** (câu trả lời ngắn, bắt buộc). Hướng dẫn thí sinh lấy mã ở mục *Đội của tôi*. Nếu đội chưa có trên hệ thống, dùng **Tên đội** và bật "Tạo đội mới" khi nhập.
2. **Thành viên / MSSV** (tùy chọn, được lưu vào ghi chú đội).
3. **Mã nguồn**: nên dùng câu hỏi **đoạn văn** để dán mã (chỉ cần CSV). Nếu dùng **tải file**, yêu cầu đặt tên file là `MÃĐỘI.py`. Sau đó vào Drive → thư mục câu hỏi → *Tải xuống* để lấy ZIP.

Nhập trong `/arena/admin` → *Nhập Google Form*: chọn CSV (và ZIP nếu có), bấm **Chạy thử**, đọc các dòng lỗi, rồi bấm **Nhập thật**. Hệ thống xử lý các trường hợp sau:

- Một đội nộp nhiều lần thì lấy dòng cuối; các dòng cũ được đánh dấu "bị thay".
- Mã trùng với bản nộp gần nhất của đội thì bỏ qua.
- Tự bỏ khối ```python, ký tự NBSP và zero-width do copy từ web/Word.
- File không UTF-8, file > 64 KiB, file HTML (tải sai từ Drive), thiếu `choose_move` đều được báo lỗi theo từng dòng; các dòng khác vẫn nhập bình thường.
- ZIP chỉ được đọc trong bộ nhớ, bỏ qua đường dẫn, giới hạn kích thước từng file và tổng dung lượng.
- Có nhiều file cùng khớp một đội thì lấy file mới nhất và kèm cảnh báo.
- "Tạo tài khoản cho đội mới" sinh mật khẩu tạm, và tài khoản bị buộc đổi mật khẩu. File tài khoản **chỉ tải được một lần trong 1 giờ**: hãy lưu riêng và gửi riêng cho từng đội.

CLI tương đương (có thêm `--dry-run`): `python -m arena_backend import-form responses.csv --files drive.zip --tournament ID --create-teams --credentials /root/tai-khoan.csv`.

## 7. Kịch bản ngày thi

| Thời điểm | Việc cần làm |
|---|---|
| T-7 ngày | Chạy thử tải trên staging, chạy smoke sandbox, tổ chức một giải kín 16–32 bot, thử backup/restore. |
| T-3 ngày | Mở chiến dịch boss để sinh viên làm quen: ai qua Boss 1 là bot đã chạy đúng. Kiểm tra bảng *Trạng thái các boss* trên admin: cả 5 boss phải “Hợp lệ”. |
| T-1 ngày | Chốt commit, image và engine. Tạo giải Swiss với hạn chọn bot, bật *Tự mở vòng*. Đăng thông báo. |
| Mở cổng | Bật đăng ký. Theo dõi chỉ số "SV chưa có đội" và nhắc lập đội. |
| Trước hạn 30 phút | Thông báo hạn chót. Hàng chờ kiểm tra sẽ dài hơn; số "Ước tính xong" cho biết còn bao lâu. |
| Sau hạn | Chờ "Bài nộp đang chờ" về 0, rồi bấm **Khóa danh sách** (hoặc **Khóa bắt buộc** để loại các bot chưa được kiểm tra). Tắt đấu xếp hạng, đấu tập/giao hữu và chiến dịch boss; backup; rồi **Mở vòng**. |
| Mỗi vòng | Xem "Job lỗi". Nếu có, bấm *Thử lại mọi job lỗi*. Vòng tiếp theo tự mở. |
| Chung kết | Tạo giải *Loại trực tiếp* với nguồn đội "Top N của" vòng Swiss: danh sách được khóa ngay với hạt giống theo hạng. |
| Kết thúc | Tải CSV bảng điểm và xếp loại. Backup. Bật lại đấu xếp hạng nếu muốn. |

Xử lý sự cố:

- **Runner chết/khởi động lại**: job đang chạy tự về hàng đợi (dừng êm) hoặc được thu hồi sau 90 giây. Container mồ côi bị dọn sau 30 giây.
- **Máy chủ quá tải hoặc DB bận**: API trả 503, và trình duyệt tự giãn chu kỳ refresh rồi thử lại. Có thể bật *Chế độ chỉ xem* để chặn mọi thao tác ghi trong lúc xử lý.
- **Gia hạn nộp bài**: tạo giải mới với hạn mới hoặc nhập thêm qua Google Form. Admin vẫn thêm được đăng ký sau hạn cho tới lúc khóa danh sách.
- **Gian lận/sao chép**: tab *Bài nộp* có ghi chú "trùng mã với N đội". Lưu ý bot mẫu nguyên bản trùng nhiều là chuyện bình thường. Dùng *Truất quyền* trên bảng điểm giải hoặc với cả đội.
- **Quên mật khẩu**: tab *Đội & tài khoản* → *Cấp lại mật khẩu* (hiện một lần, buộc đổi khi đăng nhập).

## 8. Sao lưu và khôi phục

```sh
ARENA_DATA=/var/lib/ottv2 .venv/bin/python -m arena_backend backup /duong-dan-backup/arena-$(date +%F-%H%M).sqlite3
```

Lệnh dùng SQLite backup API nên bản sao luôn nhất quán khi hệ thống đang chạy, và không ghi đè file đã có. Sao lưu trước và sau mỗi vòng. Để khôi phục: dừng API và runner, đặt bản backup thành `arena.sqlite3` trong một thư mục mới (không đặt cạnh WAL/SHM cũ), rồi đổi `ARENA_DATA`. Database V1 (mỗi người một bot) được tự nâng lên V2 khi khởi động lần đầu: mỗi người chơi cũ thành một đội một người, kết quả và Elo được giữ nguyên. Hãy backup trước khi nâng cấp.

## 9. Kiểm thử

```sh
npm ci && npm test
.venv/bin/python -m unittest discover -s arena_backend/tests -v
.venv/bin/python scripts/arena-sandbox-smoke.py --mode docker
ARENA_PYTHON=.venv/bin/python node scripts/arena-smoke.mjs
```

Test tự động không thay thế được việc rà soát bảo mật độc lập và tổng duyệt trên phần cứng thật.
