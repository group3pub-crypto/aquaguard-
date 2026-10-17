const LETTERS = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
const DIGITS = '23456789';
const ALL = LETTERS + DIGITS;

function pick(chars: string, rnd: Uint32Array, i: number): string {
  // modulo bias is negligible for a 56-char alphabet over 32-bit values
  return chars.charAt((rnd[i] ?? 0) % chars.length);
}

/** Random temporary password (default 16 chars) satisfying the policy: letter + digit, < 72 bytes. */
export function generateTempPassword(length = 16): string {
  const rnd = new Uint32Array(length);
  crypto.getRandomValues(rnd);
  const out: string[] = [];
  for (let i = 0; i < length; i++) out.push(pick(ALL, rnd, i));
  // guarantee at least one letter and one digit at random positions
  const extra = new Uint32Array(4);
  crypto.getRandomValues(extra);
  out[(extra[0] ?? 0) % length] = pick(LETTERS, extra, 1);
  let d = (extra[2] ?? 0) % length;
  const l = out.findIndex((c) => LETTERS.includes(c));
  if (d === l && length > 1) d = (d + 1) % length;
  out[d] = pick(DIGITS, extra, 3);
  if (!out.some((c) => LETTERS.includes(c))) out[0] = pick(LETTERS, extra, 1);
  return out.join('');
}
