/** Stable English notation, independent of browser locale; unavailable is never zero. */
export function compactTokens(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value < 0) return '—';
  let index = value >= 1e9 ? 3 : value >= 1e6 ? 2 : value >= 1e3 ? 1 : 0;
  const units = ['', 'K', 'M', 'B'];
  if (index === 0 && Number(value.toFixed(1)) >= 1000) index = 1;
  const scale = () => 1000 ** index;
  // Promote rounded boundaries: 999,999 becomes 1M, never 1000K.
  if (index > 0 && index < 3 && Number((value / scale()).toFixed(1)) >= 1000) index++;
  const rounded = index ? Number((value / scale()).toFixed(1)) : Number(value.toFixed(1));
  return `${rounded}${units[index]}`;
}
