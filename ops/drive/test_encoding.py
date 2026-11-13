import json
import tracemalloc
import unittest

from drive_store import encoded


class ArchiveEncodingTests(unittest.TestCase):
    def test_exact_existing_archive_bytes(self):
        value = {'z': ['Україна', 'emoji \U0001f525', '\n\t"\\', None, True, False],
                 'a': {'numbers': [-0.0, 1e-8, 1e20, 123, 0.125]}}
        expected = json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':'), allow_nan=False).encode()
        self.assertEqual(encoded(value), expected)
        with self.assertRaises(ValueError):
            encoded({'invalid': float('nan')})

    def test_archive_encoding_avoids_full_unicode_copy(self):
        # Distinct rows with mostly ASCII and one non-BMP character recreate the
        # widening that made a small UTF-8 archive require a huge dumps() string.
        value = {'items': [{'id': i, 'text': 'a' * 2000 + '\U0001f525'} for i in range(4000)]}
        tracemalloc.start()
        try:
            result = encoded(value)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, len(result) * 1.5)
        self.assertEqual(json.loads(result), value)


if __name__ == '__main__':
    unittest.main()
