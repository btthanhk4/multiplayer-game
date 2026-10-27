// Turns the technical text of a failed validation into a plain Vietnamese explanation for someone new to Python.
// Pure (no DOM) so it can be unit-tested. The raw text is always shown next to it, never replaced.

const lineOf = text => {
  const found = /line (\d+)/i.exec(text);
  return found ? Number(found[1]) : null;
};

const RULES = [
  [/IndentationError|TabError/i, log => ({
    title: 'Thụt lề (khoảng trắng đầu dòng) chưa đúng',
    hint: `Python dùng khoảng trắng đầu dòng để biết dòng nào thuộc hàm nào. Các dòng trong cùng một khối phải thụt vào bằng nhau (dùng 4 dấu cách, đừng trộn với phím Tab).${lineOf(log) ? ` Xem quanh dòng ${lineOf(log)}.` : ''}`})],
  [/SyntaxError/i, log => ({
    title: 'Lỗi cú pháp Python',
    hint: `Python không đọc nổi một dòng code. Hay gặp: thiếu dấu hai chấm “:” ở cuối dòng def/if/for, ngoặc ( [ { mở mà chưa đóng, thiếu dấu nháy, hoặc gõ nhầm từ khóa.${lineOf(log) ? ` Xem dòng ${lineOf(log)} và dòng ngay phía trên nó.` : ''}`})],
  [/ModuleNotFoundError|ImportError/i, log => {
    const name = /No module named '([^']+)'/.exec(log)?.[1];
    return {title: `Không dùng được thư viện${name ? ` “${name}”` : ' này'}`,
      hint: 'Bot chỉ được dùng thư viện có sẵn của Python (random, math, collections, itertools, heapq, functools…). Không có numpy, pandas hay thư viện cài thêm. Hãy bỏ dòng import đó và tự viết phần cần dùng.'};
  }],
  [/NameError: name '([^']+)'/i, log => ({
    title: `Dùng một tên chưa được khai báo: “${/name '([^']+)'/.exec(log)[1]}”`,
    hint: 'Có thể bạn gõ sai chính tả tên biến hoặc tên hàm, hoặc quên dòng import. Python phân biệt chữ hoa và chữ thường.'})],
  [/choose_move/i, () => ({
    title: 'Chưa có hàm choose_move',
    hint: 'Bot cần đúng một hàm tên là choose_move(observation) trả về {"from": [hàng, cột], "to": [hàng, cột]}. Kiểm tra tên hàm và dòng def.'})],
  [/object chỉ có from và to|Tọa độ phải|JSON/i, () => ({
    title: 'Bot trả kết quả sai định dạng',
    hint: 'Hàm choose_move phải trả đúng {"from": [hàng, cột], "to": [hàng, cột]}, hai tọa độ là số nguyên, không thêm khóa nào khác. Đừng dùng print() để trả kết quả.'})],
  [/KeyError|IndexError|TypeError|AttributeError|ValueError|ZeroDivisionError/i, log => {
    const kind = /(KeyError|IndexError|TypeError|AttributeError|ValueError|ZeroDivisionError)/i.exec(log)[1];
    const what = {KeyError: 'tìm một khóa không tồn tại trong dict', IndexError: 'lấy phần tử ngoài phạm vi danh sách',
      TypeError: 'dùng sai kiểu dữ liệu (ví dụ cộng số với chữ, hoặc dùng None như số)', AttributeError: 'gọi thuộc tính không có trên giá trị đó (thường là giá trị đang là None)',
      ValueError: 'đưa vào một giá trị không hợp lệ', ZeroDivisionError: 'chia cho 0'}[kind];
    return {title: `Bot bị lỗi khi chạy (${kind})`,
      hint: `Bot đã ${what}. Một ô trống trên bàn là None, nên hãy kiểm tra “if ô is not None” trước khi đọc quân trong ô đó. Dùng nút “Đấu tập” để chạy thử và xem bot dừng ở nước nào.`};
  }],
  [/vượt giới hạn|hết quỹ|quá \d+ giây/i, () => ({
    title: 'Bot suy nghĩ quá lâu',
    hint: 'Mỗi nước đi có tối đa vài giây và cả ván có một quỹ thời gian. Tránh vòng lặp không dừng và các phép tính lặp quá nhiều; chọn nước theo điểm số đơn giản thay vì thử mọi khả năng.'})],
  [/nước đi không hợp lệ|không hợp lệ/i, () => ({
    title: 'Bot trả về nước đi không nằm trong danh sách hợp lệ',
    hint: 'Nước đi phải là một trong observation["legalMoves"]. Cách an toàn nhất: chọn một phần tử của danh sách đó rồi trả về from và to của nó.'})],
  [/log quá|lớn hơn 4 KiB/i, () => ({
    title: 'Bot in ra quá nhiều chữ',
    hint: 'Xóa các lệnh print() còn sót lại; bot không cần in gì cả, chỉ cần trả về nước đi.'})],
];

export function explainBotError(log) {
  const text = String(log || '');
  for (const [pattern, build] of RULES) if (pattern.test(text)) return build(text);
  return text ? {title: 'Bot chưa qua được bước kiểm tra', hint: 'Đọc dòng chi tiết bên dưới, sửa rồi nộp lại. Nếu vẫn chưa rõ, mở “Hướng dẫn viết bot” hoặc thử bot mẫu để so sánh.'} : null;
}
