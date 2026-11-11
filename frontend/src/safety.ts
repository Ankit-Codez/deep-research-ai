/** Conservative public-link syntax check; retrieval/DNS safety stays in Python. */
export function safeSourceURL(value: string | undefined | null): string | null {
  if (!value || value.length > 4096 || /[\s\\\x00-\x1f\x7f]/.test(value)) return null;
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase().replace(/\.$/, '');
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.port) return null;
    // IP literals and local network names never become dashboard links.
    if (!host.includes('.') || host.includes(':') || /^[\d.]+$/.test(host)
        || /(^|\.)(localhost|local|internal|home|lan)$/.test(host)) return null;
    url.hash = '';
    return url.href;
  } catch { return null; }
}

/** Decode common title entities into plain React text, without parsing HTML. */
export function displaySourceTitle(value: string): string {
  const named: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
  for (let pass = 0; pass < 2; pass++) {
    value = value.replace(/&(#x[0-9a-f]+|#[0-9]+|amp|lt|gt|quot|apos|nbsp);/gi, (entity, code: string) => {
      if (!code.startsWith('#')) return named[code.toLowerCase()] ?? entity;
      const hex = code.slice(0, 2).toLowerCase() === '#x';
      const point = Number.parseInt(code.slice(hex ? 2 : 1), hex ? 16 : 10);
      return point > 0 && point <= 0x10ffff && !(point >= 0xd800 && point <= 0xdfff)
        ? String.fromCodePoint(point) : '\ufffd';
    });
  }
  return value.replace(/[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]/g, '').trim();
}
