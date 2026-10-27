// "Bot không cần code": turns four sliders into the sdk/examples/custom.py template by rewriting its constants.
// Pure (no DOM) so it can be unit-tested; the template itself is a real, tested Python file served to the page.

export const FIELDS = [
  {key: 'CHASE_GOAL', label: 'Chạy về ô đích', min: 0, max: 10, low: 'thong thả', high: 'lao thẳng về đích', hint: 'Càng cao, bot càng ưu tiên đưa quân tới ô đích (đi vòng vật cản cho đúng đường).'},
  {key: 'CAPTURE', label: 'Ăn quân đối phương', min: 0, max: 30, low: 'bỏ qua', high: 'săn quân', hint: 'Càng cao, bot càng thích ăn quân (Búa ăn Kéo, Kéo ăn Lá, Lá ăn Búa).'},
  {key: 'SAFETY', label: 'Tránh bị khắc chế', min: 0, max: 30, low: 'liều', high: 'cẩn thận', hint: 'Càng cao, bot càng tránh bước vào ô cạnh quân có thể ăn mình.'},
  {key: 'RANDOMNESS', label: 'Khó đoán', min: 0, max: 20, low: 'luôn nước tốt nhất', high: 'hay đổi nước', hint: 'Số lớn làm bot thỉnh thoảng đi nước khác, khó bị đối thủ đoán trước.'},
];

export const PRESETS = [
  {id: 'balanced', label: 'Cân bằng', note: 'Vừa tiến vừa ăn, tránh chỗ nguy hiểm', values: {CHASE_GOAL: 1, CAPTURE: 12, SAFETY: 10, RANDOMNESS: 1}},
  {id: 'attack', label: 'Tấn công', note: 'Ưu tiên ăn quân, ít để ý nguy hiểm', values: {CHASE_GOAL: 1, CAPTURE: 25, SAFETY: 4, RANDOMNESS: 1}},
  {id: 'rush', label: 'Về đích nhanh', note: 'Lao về ô đích, ăn quân khi tiện', values: {CHASE_GOAL: 6, CAPTURE: 6, SAFETY: 6, RANDOMNESS: 1}},
  {id: 'guard', label: 'Phòng thủ', note: 'Giữ quân an toàn, đợi sơ hở', values: {CHASE_GOAL: 1, CAPTURE: 10, SAFETY: 26, RANDOMNESS: 1}},
  {id: 'tricky', label: 'Khó đoán', note: 'Cân bằng nhưng hay đổi nước', values: {CHASE_GOAL: 2, CAPTURE: 12, SAFETY: 10, RANDOMNESS: 12}},
];

export const clamp = (field, value) => {
  const number = Math.round(Number(value));
  return Number.isFinite(number) ? Math.min(field.max, Math.max(field.min, number)) : field.min;
};

// Rewrites `NAME = <integer>` lines of the template. Throws when the template has changed shape, so the page can fall
// back to showing the plain template instead of silently producing a bot that ignores the sliders.
export function buildBot(template, values) {
  let source = String(template);
  for (const field of FIELDS) {
    const pattern = new RegExp(`^(${field.key}\\s*=\\s*)\\d+`, 'm');
    if (!pattern.test(source)) throw new Error(`Mẫu bot thiếu dòng ${field.key}.`);
    source = source.replace(pattern, (_, head) => `${head}${clamp(field, values[field.key])}`);
  }
  return source;
}

export const readValues = template => Object.fromEntries(FIELDS.map(field => {
  const found = new RegExp(`^${field.key}\\s*=\\s*(\\d+)`, 'm').exec(String(template));
  return [field.key, found ? clamp(field, found[1]) : PRESETS[0].values[field.key]];
}));
