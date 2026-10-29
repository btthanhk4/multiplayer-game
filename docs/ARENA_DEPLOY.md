# OTTv2 Arena — triển khai và vận hành cho ~1.000 tân sinh viên

## Có gì trong V2

- **Tài khoản và đội**: sinh viên tự đăng ký bằng mã tham gia; tạo đội hoặc vào đội bằng mã 8 ký tự (mặc định tối đa 4 người, `ARENA_TEAM_SIZE`). Bot, Elo và đăng ký giải thuộc về đội.
- **Dashboard sinh viên** (`/`): lộ trình 5 bước, thẻ đội/bot/Elo/giải đang mở, vị trí trong hàng chờ, bảng điểm có tìm kiếm, xem từng vòng, khán đài 4 bàn kèm replay.
- **Dashboard ban tổ chức** (`/arena/admin`): số liệu hệ thống và ước tính thời gian chấm, công tắc sự kiện (đăng ký, nộp bài, xếp hạng, đấu tập, chế độ chỉ xem), thông báo toàn trang, quản lý giải, tìm và xử lý đội/tài khoản (cấp lại mật khẩu, khóa, xóa), xem mã nghi sao chép, hàng đợi, nhật ký.
- **Thể thức**: Swiss (≤ 2.000 đội), vòng tròn (≤ 32), loại trực tiếp (≤ 1.024, có thể lấy top N của giải trước). Có tự mở vòng, truất quyền giữa giải (xử thua các ván chưa đấu) và xếp loại Xuất sắc/Giỏi/Khá/Đạt. Bảng điểm xuất ra CSV.
- **Máy chấm**: mỗi bot một sandbox cho cả ván (giao thức 2). Có nhiều luồng chấm song song (`--slots`), hàng đợi ưu tiên (kiểm tra bot → giải → xếp hạng → đấu tập), tự chạy lại lỗi hạ tầng tối đa 3 lần, và trả job về hàng đợi khi runner dừng.
- **Tạo động lực (V3)**: chiến dịch 5 boss mạnh dần (bot trong `sdk/examples` và `sdk/bosses`, tự đăng ký và được kiểm tra khi API khởi động), bảng vàng chiến dịch, 17 huy hiệu, bảng tin trực tiếp (lật kèo, thắng trắng, hạ boss, vô địch), hồ sơ công khai của đội kèm đồ thị Elo, giao hữu với đội bất kỳ, bục trao giải, replay có link chia sẻ (`/#watch=<id>`), tua nhanh và phóng to, cùng pháo hoa khi đội thắng.
- SQLite WAL, kết nối dùng lại theo luồng, cache 2–3 giây cho dữ liệu công khai, và giới hạn tần suất phù hợp với việc cả trường dùng chung một IP NAT.

**Đăng ký mở**: mặc định server public đòi `ARENA_REGISTRATION_CODE`. Muốn ai có link cũng tự tạo tài khoản được, để trống mã đó và đặt `ARENA_OPEN_REGISTRATION=1`. Giới hạn tần suất vẫn áp dụng; admin có thể tắt đăng ký ở tab Tổng quan. Tab *Trận đấu* của admin lọc/hủy ván và xóa hàng chờ đấu tập/giao hữu/chiến dịch khi máy chấm quá tải.

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

**Máy chấm là nút cổ chai thật sự.** Số liệu dưới đây đo bằng `scripts/arena-load.py --storm` trên server thật của sự kiện (12 lõi dùng chung với Jenkins, Docker + gVisor, `--slots 8`), trong một container tạm với database tạm. Kịch bản: 1.000 người đăng ký, mỗi người một đội nộp bot **khác nhau cùng lúc**, rồi cả 1.000 đội cùng bấm tìm đối thủ xếp hạng. Bot là bot Thợ săn (ván ngắn, vài chục nước).

| Bước | Kết quả |
|---|---|
| 1.000 lượt đăng ký | 19 giây, 0 lỗi |
| 1.000 bot khác nhau nộp cùng lúc | 10 giây; API p95 ≈ 1,2 giây |
| Mọi bot được kiểm tra xong | trung bình 91 giây, **tối đa 171 giây** |
| 1.000 bot mẫu trùng nhau (kiểm tra một lần, dùng chung) | tối đa 11 giây |
| 1.000 đội cùng ghép trận (500 cặp = 1.000 ván) | ghép xong sau 8 giây; có kết quả trung bình 103 giây, **tối đa 191 giây** |
| Tải API trong lúc chấm (mỗi đội hỏi lại mỗi vài giây) | p95 < 1,3 giây, 0 lỗi |

Trần thật là tốc độ **khởi động container của Docker** (khoảng 6 việc chấm mỗi giây): đo riêng bằng `scripts/arena-bench.py`, 6 luồng cho 5,4 bot/giây và 4,3 ván/giây, còn 12 luồng chỉ nhanh hơn khoảng 18% (6,4 và 5,2). Vì vậy đừng đặt `--slots` quá số lõi trống; 8 là hợp lý khi máy còn chạy việc khác.

Bot thật nghĩ lâu hơn bot mẫu: mỗi ván dài tới 2 phút ở trường hợp xấu nhất (quỹ 60 giây suy nghĩ mỗi bên). Khi đó thông lượng giảm theo, xấp xỉ `số luồng ÷ thời gian trung bình của một ván`. Ví dụ ván trung bình 20 giây với 8 luồng cho khoảng 0,4 ván/giây: 1.000 ván hết khoảng 42 phút. Trang admin hiện "Ước tính xong" theo thời gian chấm thực tế của giờ gần nhất; sinh viên cũng thấy ước tính chờ của riêng mình.

**Chống tắc nghẽn (đã bật sẵn):**

- **Thứ tự chấm:** kiểm tra bot luôn đầu tiên; sau đó giải đấu, xếp hạng, rồi chiến dịch/giao hữu/đấu tập. Một ván chờ càng lâu càng được đẩy lên (mỗi 5 phút lên một bậc, tối đa 1,5 bậc), nên ván chiến dịch và đấu tập không bị bỏ đói khi xếp hạng đông.
- **Giới hạn hàng chờ:** khi đã có `ARENA_QUEUE_CAP_RANKED` (mặc định 4.000) ván xếp hạng hoặc `ARENA_QUEUE_CAP_LOW` (mặc định 2.000) ván đấu tập/giao hữu/chiến dịch đang chờ, yêu cầu mới bị từ chối kèm thông báo "máy chấm đang quá tải, thử lại sau ít phút". Ván của giải đấu không bao giờ bị từ chối.
- **Nộp trùng mã:** nhiều đội nộp cùng một bot mới trong lúc đang kiểm tra thì chỉ kiểm tra một lần; các đội còn lại nhận chung kết quả (kể cả khi phải thử lại sau sự cố).
- **Xem trực tiếp là phần phụ:** mỗi người xem tối đa 2 bàn, cập nhật 2–3 giây một lần. Máy chấm tự ngừng ghi tiến độ từng nước khi hàng chờ trên 150 việc. Admin có công tắc *Cho xem ván trực tiếp* ở tab Tổng quan.
- **Bản phát lại gọn:** mỗi ván lưu danh sách nước đi, không lưu lịch sử trùng lặp (giảm khoảng 12 KB mỗi ván).

Khi hàng chờ vẫn quá dài: (1) tắt *Mở đấu xếp hạng* hoặc *Mở chiến dịch boss*; (2) ở tab *Trận đấu* bấm "Xóa hàng chờ" cho đấu tập/giao hữu/chiến dịch; (3) tăng `--slots` nếu còn lõi trống; (4) dành riêng máy chấm cho giải bằng cách tắt mọi chế độ luyện tập khi một vòng giải đang chạy.

Chiến dịch boss và giao hữu cũng tốn máy chấm: mỗi lượt là 2 ván, ưu tiên ngang đấu tập (sau giải và xếp hạng, nhưng được đẩy lên khi chờ lâu). Mỗi đội chỉ có một lượt boss và một lượt giao hữu chạy cùng lúc, và có giới hạn 30 lượt boss/giờ (`ARENA_RATE_CAMPAIGN_TEAM`). Nếu 1.000 đội cùng leo tháp, hàng đợi có thể dài vài giờ; khi đó hãy tắt *Mở chiến dịch boss* hoặc tăng `--slots`. Boss 5 nghĩ tới ~0,6 giây mỗi nước, nên ván với boss 5 dài hơn ván thường.

Khuyến nghị: dùng máy API 4 lõi / 8 GB. Mỗi máy runner có bao nhiêu lõi trống thì đặt `--slots` bằng chừng đó (mỗi sandbox `--cpus=1`, `--memory=256m`, nên 8 slot cần khoảng 4 GB RAM cho bot). Nhiều runner có thể dùng chung một database **trên cùng máy**. Không đặt SQLite lên ổ mạng; nếu cần runner ở máy khác, phải thiết kế lại (ví dụ chuyển sang PostgreSQL). Admin dashboard hiển thị "Ước tính xong" dựa trên thời gian chấm thực tế của giờ gần nhất.

Trước sự kiện, chạy thử tải trên **máy staging giống hệt production** (hoặc dùng cách trong container tạm như trên, không bao giờ trỏ vào database thật):

```sh
python scripts/arena-load.py --base https://staging.example.org --users 1000 --invite MÃ --unique-bots
```

## 3. Hợp đồng bot và thể lệ

Xem `/arena/guide.html`. Tóm tắt: `choose_move(observation)` trả `{from: [r,c], to: [r,c]}`. Mỗi bot được nạp một lần mỗi ván và giữ biến toàn cục trong ván. Giới hạn: 3 giây/lượt, quỹ 60 giây suy nghĩ mỗi ván, 5 giây nạp mã, 256 MiB RAM, 4 KiB kết quả, 1 MiB log, 400 nửa lượt (đủ thì xử ván). Sandbox chặn mạng, đặt rootfs chỉ đọc, `cap-drop ALL`, `no-new-privileges`, `RLIMIT_NPROC=1` sau khi adapter khởi động, giới hạn PID cgroup 64 và CPU tích lũy bằng quỹ thời gian + 20 giây. `print()` của bot được chuyển sang stderr nên không thể làm hỏng giao thức.

**Luật xử ván (giảm hòa).** Ván hết thế (lặp thế ba lần, 80 nửa lượt không ăn quân, đủ 400 nửa lượt, hoặc phe đến lượt hết nước đi) không còn hòa ngay: bên nhiều quân hơn thắng, bằng quân thì bên có quân gần ô đích hơn thắng, vẫn bằng thì mới hòa. Đo với các bot mẫu và boss, tỉ lệ hòa giảm từ khoảng 22% xuống 0–3%, nên bảng điểm Swiss ít bị dồn đồng điểm. Lưu ý: bên đi sau có lợi thế đáng kể trên bản đồ chuẩn (hai bot giống nhau tự đấu: đi sau thắng khoảng 65%, đi trước khoảng 26%); vì mỗi cặp luôn gồm hai ván đổi màu nên lợi thế này triệt tiêu ở cấp cặp và cấp giải.

**Bắt đầu giải và giải riêng.** Nút **▶ Bắt đầu giải** ở tab *Giải đấu* đóng đăng ký, khóa danh sách và mở vòng 1 trong một giao dịch, kể cả trước hạn đăng ký: nếu thiếu 2 bot hợp lệ, bot còn chờ kiểm tra (cần xác nhận loại), hoặc engine đã đổi, giải giữ nguyên trạng thái cũ. Hai admin bấm cùng lúc thì chỉ một lần được chấp nhận. Các nút *Khóa danh sách* và *Mở vòng* vẫn có trong mục gập *Từng bước thủ công*. Khi tạo giải bật **Giải riêng**, hệ thống sinh mã 6 ký tự (bỏ các ký tự dễ nhầm 0/O, 1/I): đội phải nhập mã để vào giải, mã không phân biệt hoa thường và bỏ qua dấu cách hoặc gạch ngang; đội đã vào được đổi bot không cần mã; admin thêm đội thủ công không cần mã; đoán sai quá 20 lần trong 15 phút thì bị chặn tạm. Mã chỉ hiện cho admin (đổi hoặc bỏ mã khi giải còn nhận đăng ký). Đội đã đăng ký thấy *Phòng chờ* cho tới khi giải bắt đầu và không cần bấm tìm trận.

**Bản đồ.** Mỗi giải có một bản đồ, và vòng chung kết có thể có bản đồ riêng. Có 10 bản đồ: *Chuẩn*, *Bức tường*, *Hai cánh*, *Khối vuông* (chỉ đổi cách xếp quân, bàn 9×9), *Đấu trường 8×8*, *Bốn trụ*, *Dòng sông*, *Mê cung 8×8* (đổi kích thước bàn hoặc có vật cản), *Ngẫu nhiên* (mỗi cặp một cách xếp) và *Hỗn loạn* (mỗi cặp một bàn 8×8 hoặc 9×9 với vật cản ngẫu nhiên; hai ván của cặp dùng chung). Luật đi quân và cách thắng không đổi; mọi bản đồ đối xứng quay 180° nên công bằng, và bàn luôn liền một khối nên luôn có đường tới đích. Quan trọng: bot viết cứng bàn 9×9 hoặc ô đích `[0, 8]` sẽ thua kỹ thuật trên bản đồ 8×8/vật cản, vì vậy chỉ dùng các bản đồ này sau khi đã thông báo cho thí sinh đọc Sổ tay mục 2.6 (bot nhận `size`, `goal`, `opponentGoal`, `obstacles`) và cho họ thử ở mục Đấu tập. Form tạo giải tự hiện cảnh báo khi chọn các bản đồ này. Đề xuất: vòng loại Swiss dùng *Chuẩn* hoặc *Ngẫu nhiên*; vòng trong dùng một bản đồ vật cản đã công bố (ví dụ *Dòng sông*); chung kết dùng *Chuẩn* hoặc *Hỗn loạn*. Xếp hạng và chiến dịch luôn dùng *Chuẩn* để Elo ổn định. Admin đổi bản đồ giữa các vòng ở tab *Giải đấu* (chỉ áp dụng cho vòng mở sau đó). Xử hết thế tính khoảng cách tới đích bằng đường đi vòng vật cản. Đổi luật giữa chừng thì **không hỗ trợ**.

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

Trỏ hostname về `http://127.0.0.1:8080`. Đặt `ARENA_ORIGIN=https://…` đúng với URL trình duyệt dùng, và bật `ARENA_TRUST_CLOUDFLARE=1` khi API chỉ bind loopback, để giới hạn tần suất dùng IP thật. Nếu cloudflared gọi API qua địa chỉ LAN của chính máy chủ (ví dụ `http://192.168.10.185:8091`), hãy đặt `ARENA_TRUSTED_PROXIES=192.168.10.185`: thiếu biến này, mọi người vào qua Cloudflare bị tính chung **một** IP, và một người spam có thể dùng hết hạn mức đăng ký/đăng nhập của cả trường. Chỉ request đến từ đúng địa chỉ đó mới được tin header `CF-Connecting-IP`. Mã mời (`ARENA_REGISTRATION_CODE`) là bắt buộc khi public. Đây là lớp chặn chính, vì giới hạn theo IP được đặt rộng (mặc định 3.000 lượt đăng ký/giờ/IP) cho cả trường chung NAT. Giới hạn theo tài khoản/đội vẫn chặt: 20 lần đăng nhập sai/15 phút cho mỗi tài khoản từ mỗi địa chỉ (và 100 lần từ mọi địa chỉ cộng lại, để người khác không khóa được tài khoản của bạn chỉ bằng 20 lần đoán sai), 20 bài nộp/giờ/đội, tối đa 3 bài chờ kiểm tra/đội. Tất cả đều chỉnh được bằng biến `ARENA_RATE_*`.

## 6. Nộp bài

Chỉ có một đường nộp: **sinh viên nộp trên web** (tab *Viết & kiểm tra*). Đường này đã được thiết kế và đo cho 1.000 người nộp cùng lúc (xem mục 2): nhận bài chỉ ghi database, việc kiểm tra xếp hàng ở máy chấm, bot trùng mã chỉ kiểm tra một lần.

Cách hỗ trợ sát hạn:

- Đội lỡ hạn đăng ký giải: admin dùng *Thêm đội thủ công* ở tab *Giải đấu* (dùng bot hợp lệ mới nhất của đội).
- Đội không vào được: admin cấp lại mật khẩu ở tab *Đội & tài khoản*, hoặc tạo lại tài khoản (xóa tài khoản sai, để sinh viên đăng ký lại).
- Muốn dừng nộp bài tạm thời: tắt công tắc *Cho nộp bot trên web* ở tab *Tổng quan*.

## 7. Kịch bản ngày thi

| Thời điểm | Việc cần làm |
|---|---|
| T-7 ngày | Chạy thử tải trên staging, chạy smoke sandbox, tổ chức một giải kín 16–32 bot, thử backup/restore. |
| T-3 ngày | Mở chiến dịch boss để sinh viên làm quen: ai qua Boss 1 là bot đã chạy đúng. Kiểm tra bảng *Trạng thái các boss* trên admin: cả 5 boss phải “Hợp lệ”. |
| T-1 ngày | Chốt commit, image và engine. Tạo giải Swiss với hạn chọn bot, bật *Tự mở vòng*. Đăng thông báo. |
| Mở cổng | Bật đăng ký. Theo dõi chỉ số "SV chưa có đội" và nhắc lập đội. |
| Trước hạn 30 phút | Thông báo hạn chót. Hàng chờ kiểm tra sẽ dài hơn; số "Ước tính xong" cho biết còn bao lâu. |
| Cần đổi giải | Tab *Giải đấu*: sửa tên/mô tả/hạn/số vòng Swiss, **Đóng đăng ký ngay**, thêm đội thủ công bằng mã đội hoặc tên đội (đội lỡ hạn), xóa đội khỏi danh sách trước khi khóa, xem từng cặp đấu theo vòng, hoặc **Hủy giải** (hủy mọi ván chưa xong). |
| Sau hạn | Chờ "Bài nộp đang chờ" về 0, rồi bấm **Khóa danh sách** (hoặc **Khóa bắt buộc** để loại các bot chưa được kiểm tra). Tắt đấu xếp hạng, đấu tập/giao hữu và chiến dịch boss; backup; rồi **Mở vòng**. |
| Mỗi vòng | Xem "Job lỗi". Nếu có, bấm *Thử lại mọi job lỗi*. Vòng tiếp theo tự mở. |
| Chung kết | Tạo giải *Loại trực tiếp* với nguồn đội "Top N của" vòng Swiss: danh sách được khóa ngay với hạt giống theo hạng. Chọn bản đồ cho vòng trong (ví dụ *Ngẫu nhiên*) và thông báo trước cho sinh viên. |
| Kết thúc | Tải CSV bảng điểm và xếp loại. Backup. Bật lại đấu xếp hạng nếu muốn. |

Xử lý sự cố:

- **Runner chết/khởi động lại**: job đang chạy tự về hàng đợi (dừng êm) hoặc được thu hồi sau 90 giây. Container mồ côi bị dọn sau 30 giây.
- **Máy chủ quá tải hoặc DB bận**: API trả 503, và trình duyệt tự giãn chu kỳ refresh rồi thử lại. Có thể bật *Chế độ chỉ xem* để chặn mọi thao tác ghi trong lúc xử lý.
- **Gia hạn nộp bài**: kéo dài hạn ở tab *Giải đấu* (sửa hạn đăng ký), hoặc thêm đội thủ công. Admin vẫn thêm được đăng ký sau hạn cho tới lúc khóa danh sách.
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
