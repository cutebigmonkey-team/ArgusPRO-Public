export const FORMAT = 'argus-drive-catalog-v1';
export const fileId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{10,200}$/.test(value);
export function captureLabel(item) {
  if (item.processing_status === 'metadata_only') return 'Публічне прев’ю недоступне. Збережено метадані.';
  if (item.capture_mode === 'search_preview') return 'Лише пошукове прев’ю. Повний текст не отримано.';
  return '';
}
export function validateCatalog(value) {
  const digest = s => typeof s === 'string' && /^[a-f0-9]{64}$/.test(s);
  const legacy = value?.format === FORMAT && fileId(value.snapshot_id);
  const parts = value?.snapshot_parts;
  const partitioned = value?.format === 'argus-drive-catalog-v2' && Array.isArray(parts) &&
    parts.length > 0 && parts.length <= 16 && parts.every(p => p && fileId(p.id) && digest(p.sha256) &&
      Number.isSafeInteger(p.size) && p.size > 0 && p.size <= 8*1024*1024) &&
    Number.isSafeInteger(value.posts) && value.posts >= 0 &&
    Number.isSafeInteger(value.packages) && value.packages >= 0;
  if ((!legacy && !partitioned) || !digest(value.snapshot_sha256)) throw new Error('Невірний формат каталогу.');
  return value;
}

export async function readSnapshot(catalog, read, checksum) {
  validateCatalog(catalog);
  let bytes;
  if (catalog.format === FORMAT) {
    bytes = await read(catalog.snapshot_id, 20*1024*1024);
  } else {
    bytes = new Uint8Array(catalog.snapshot_parts.reduce((sum,p) => sum+p.size, 0));
    let offset = 0;
    for (let i = 0; i < catalog.snapshot_parts.length; i += 2) {
      const pair = catalog.snapshot_parts.slice(i,i+2);
      const chunks = await Promise.all(pair.map(async part => {
        const chunk = await read(part.id,part.size);
        if (chunk.byteLength !== part.size || await checksum(chunk) !== part.sha256)
          throw new Error('Не збігається контрольна сума частини архіву.');
        return chunk;
      }));
      for (const chunk of chunks) {
        bytes.set(chunk,offset);
        offset += chunk.byteLength;
      }
    }
  }
  if (await checksum(bytes) !== catalog.snapshot_sha256) throw new Error('Не збігається контрольна сума індексу.');
  const snapshot = validateSnapshot(JSON.parse(new TextDecoder().decode(bytes)));
  if (catalog.format !== FORMAT && (catalog.posts !== snapshot.items.length || catalog.packages !== snapshot.packages.length))
    throw new Error('Неповний індекс архіву.');
  return snapshot;
}
export function validateSnapshot(value, uniqueIds=true) {
  if (!value || !Array.isArray(value.items) || !Array.isArray(value.packages)) throw new Error('Невірний індекс архіву.');
  const ids = new Set();
  for (const item of value.items) {
    if (!item || typeof item.id !== 'string' || (uniqueIds && ids.has(item.id)) || typeof item.text !== 'string' ||
        typeof item.platform !== 'string' || !Array.isArray(item.files)) throw new Error('Пошкоджений запис архіву.');
    ids.add(item.id);
    for (const f of item.files) if (!fileId(f.drive_id) || !/^[a-f0-9]{64}$/.test(f.sha256) ||
        !Number.isSafeInteger(f.size) || f.size < 0 || typeof f.name !== 'string') throw new Error('Пошкоджений опис вкладення.');
  }
  return value;
}
export function filterItems(items, query = '', platform = '', sample = 'all') {
  const search = query.trim().toLocaleLowerCase('uk-UA').slice(0,200);
  return items.filter(p => (!platform || p.platform === platform) &&
    (sample === 'all' || Boolean(p.sample) === (sample === 'demo')) &&
    (!search || [p.text,p.platform,p.id].join(' ').toLocaleLowerCase('uk-UA').includes(search)));
}
export function summarize(items) {
  const platforms = {}, days = {};
  const files = new Map();
  for (const item of items) {
    platforms[item.platform] = (platforms[item.platform] || 0) + 1;
    const date = String(item.published_at || '').slice(0,10);
    if (/^\d{4}-\d{2}-\d{2}$/.test(date)) days[date] = (days[date] || 0) + 1;
    for (const f of item.files) files.set(f.sha256,f.size);
  }
  return {posts:items.length,sources:Object.keys(platforms).length,media:files.size,
    bytes:[...files.values()].reduce((a,b)=>a+b,0),platforms,days};
}
