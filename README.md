# OTTv2

## Đấu trường bot Python — V1

Repo này bổ sung nền tảng thi đấu bằng code tại **`/arena/`**, bên cạnh game người chơi hiện có. Backend Python + SQLite chạy trên một máy; có thể public qua Cloudflare Tunnel khi có server. Không bắt buộc Workers, D1 hoặc R2.

- Đăng nhập, nộp phiên bản Python riêng tư, kiểm tra bot, chọn phiên bản trước hạn.
- Trận tập hai ván đổi màu; giải vòng tròn hoặc Swiss theo điểm; khóa hash bài thi, bảng điểm và điều hành.
- Khán đài bốn bàn, cuộn trên mobile, replay đầy đủ và xem lại trận kết thúc.
- Runner tách khỏi API; production yêu cầu Docker + gVisor. Có chế độ thử nguyên bản bot mẫu khi chưa có server.

**Bắt đầu trên Windows** (Python 3.10+, Node.js 20+):

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r arena_backend/requirements.txt
.\.venv\Scripts\python -m arena_backend admin admin
.\.venv\Scripts\python -m arena_backend serve
```

Mở **http://localhost:8080/arena/**. Terminal thứ hai:

```powershell
.\.venv\Scripts\python -m arena_backend worker --mode samples
```

Trong Xưởng bot, chọn bot mẫu và nộp nguyên bản để thử trọn luồng. Chế độ `samples` không chạy mã tùy chỉnh trên host. Để thử Docker, triển khai Linux, cài gVisor, cấu hình Cloudflare, sao lưu và chạy test: xem **[hướng dẫn triển khai](docs/ARENA_DEPLOY.md)**. Giao thức và thể lệ: **[sổ tay bot](arena/guide.html)**.

V1 đã có luồng chức năng và kiểm thử phát triển; cần kiểm tra gVisor, đánh giá bảo mật và thử tải trên server thực trước khi tổ chức giải công khai. Có [cấu hình Docker riêng để chạy thử trên server](deploy/README.md); cấu hình đó chỉ bind API vào loopback và chưa phải triển khai công khai. Kế hoạch PDF trước đó là định hướng; phạm vi V1 và các giới hạn thực tế được ghi trong hướng dẫn triển khai.

---

Trò chơi chiến thuật Đấm · Lá · Kéo trên bàn cờ 9×9. Hai người có thể chơi chung một máy hoặc vào cùng một phòng online; người vào sau hai ghế có thể xem trận đấu.

## Chơi

- Mỗi lượt đi một quân tới một trong tám ô kề cạnh hoặc chéo.
- Đấm ăn Kéo, Kéo ăn Lá, Lá ăn Đấm. Quân đồng minh và quân đối thủ cùng loại chặn ô.
- Thắng khi đưa quân Xanh tới **i9**, quân Đỏ tới **a1**, hoặc ăn hết cả ba quân thuộc cùng một loại của đối phương.
- Ván hòa nếu thế cờ lặp lại ba lần hoặc 80 lượt liên tiếp không có quân bị ăn.
- Mỗi bên có chín quân, ba quân mỗi loại. Đội hình ban đầu đối xứng qua tâm bàn cờ.

## Chạy tại máy

Mở `index.html` qua một máy chủ HTTP tĩnh, ví dụ:

```sh
python -m http.server 8000
```

Truy cập `http://localhost:8000`. Chế độ offline chạy hoàn toàn trên trình duyệt.

Để chạy máy chủ phòng chơi có xác nhận nước đi:

```sh
npm install
npm run server
```

Khi mở site ở `localhost`, game tự kết nối PartyKit cục bộ ở cổng `1999`. Có thể chỉ định host khác bằng `?server=host:port`. Mỗi phòng có mã riêng; hai người đầu tiên vào là hai phe, người tiếp theo xem trận. Máy chủ lưu trạng thái trận, kiểm tra lượt và nước đi, cho phép kết nối lại, rời ghế và đề nghị chơi ván mới.

Trên GitHub Pages, chế độ online dùng [PlayHTML](https://playhtml.fun/) để đồng bộ bàn cờ và presence qua dịch vụ của thư viện. Có thể kiểm thử đường này ở máy cục bộ bằng `?backend=playhtml`. Vì trạng thái PlayHTML được ghi từ trình duyệt, đường này phù hợp chơi với bạn bè; máy chủ PartyKit là đường có xác nhận nước đi để triển khai riêng khi có host.

Trang [xem trận](https://btthanhk4.github.io/OTTv2/watch.html) hiển thị các phòng đang có người chơi bằng presence của PlayHTML. Chọn nhiều phòng để theo dõi cùng lúc và cuộn trang để xem các bàn tiếp theo. Các khung xem mở với vai trò khán giả, không tự chiếm ghế và không gửi thao tác chơi. Trận kết thúc tự rời khỏi khán đài; phòng không còn người chơi biến mất khỏi danh sách.

Nút **Ghép trận nhanh** tìm phòng đang có đúng một người chơi qua sảnh PlayHTML. Nếu chưa có phòng phù hợp, người chơi ở màn hình chờ và có thể hủy. Khi một người khác cũng vào hàng chờ, họ được ghép vào cùng một bàn cờ. Nếu ghế vừa được người khác chiếm, ghép trận sẽ quay lại sảnh chờ để tìm phòng khác.

## Kiểm thử

```sh
npm test
```

`node scripts/browser-smoke.mjs` kiểm tra giao diện, offline và PartyKit khi cả HTTP tĩnh và PartyKit đang chạy. `node scripts/playhtml-smoke.mjs` kiểm tra hai người chơi và khán giả qua PlayHTML khi HTTP tĩnh đang chạy. Bài kiểm tra trình duyệt dùng Microsoft Edge cài trên máy.

Để kiểm tra trang đã xuất bản: `node scripts/playhtml-smoke.mjs https://btthanhk4.github.io/OTTv2/`.
`node scripts/watch-smoke.mjs` kiểm tra sảnh trận, cuộn xem hơn bốn khung, tự gỡ trận kết thúc và kích thước ô cờ. `node scripts/matchmaking-smoke.mjs` kiểm tra trạng thái chờ, ghép vào ghế có sẵn, ghép hai người đang chờ và hủy tìm trận.
