"""Explicit synthetic end-to-end fixture. Never presented as collected posts."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from storage import pack


def create(output):
    # One tiny PNG shared by two synthetic posts tests content deduplication.
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZ9kAAAAASUVORK5CYII=')
    sha = hashlib.sha256(png).hexdigest()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'media').mkdir()
        (root / 'media' / 'test.png').write_bytes(png)
        posts = [{'canonical_key': f'sample:argus:{i}', 'platform': 'test',
                  'post_url': f'https://example.invalid/argus-test/{i}',
                  'text': f'Тест імпорту {i}; не зібрані дані. Quotes: \' \\ tabs\t newlines\n',
                  'published_at': '2026-01-01T00:00:00Z', 'observed_at': '2026-01-02T00:00:00Z',
                  'media': [sha]} for i in (1, 2)]
        (root / 'posts.jsonl').write_text(''.join(json.dumps(p, ensure_ascii=False) + '\n' for p in posts), encoding='utf-8')
        return pack(root / 'posts.jsonl', root / 'media', output, sample=True)


if __name__ == '__main__':
    print(create(sys.argv[1]))
