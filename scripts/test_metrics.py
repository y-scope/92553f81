"""Regression checks for dataset fidelity and benchmark result validation.

Run with: .venv/bin/python -m unittest discover -s scripts -p 'test_*.py'
"""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from benchmark_metrics import check_result, percentile, render
from datasets.prepare_westermo import convert, matches_source, numeric


class DatasetFidelityTests(unittest.TestCase):
    def test_missing_is_distinct_from_zero_and_large_integers_remain_exact(self):
        self.assertIsNone(numeric(''))
        self.assertIsNone(numeric('NaN'))
        self.assertEqual(numeric('0'), 0)
        self.assertEqual(numeric('18446744073709551615'), 2**64 - 1)
        self.assertIs(type(numeric('18446744073709551615')), int)
        with self.assertRaises(ValueError):
            numeric('Infinity')

    def test_csv_conversion_keeps_rows_values_and_source_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / 'system-8.csv', root / 'system-8.ndjson'
            source.write_text('timestamp,load-1m,sys-mem-total\n'
                              '0,0,18446744073709551615\n30,,0\n30,1.5,0\n')
            metadata = convert(source, target)
            records = [json.loads(line) for line in target.read_text().splitlines()]
            self.assertEqual(metadata['rows'], 3)
            self.assertEqual(records[0], {'system_id': 'system-8', 'timestamp': 0,
                                         'load_1m': 0, 'sys_mem_total': 2**64 - 1})
            self.assertNotIn('load_1m', records[1])
            self.assertEqual(records[1]['sys_mem_total'], 0)
            self.assertEqual([r['timestamp'] for r in records], [0, 30, 30])

    def test_colliding_names_and_missing_timestamp_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root / 'system-1.csv', root / 'output.ndjson'
            for text in ['timestamp,a-b,a_b\n0,1,2\n', 'timestamp,load-1m\n,1\n']:
                source.write_text(text)
                with self.assertRaises(ValueError):
                    convert(source, target)

    def test_source_checksum_detects_equal_length_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'input.csv'
            path.write_bytes(b'hello')
            expected = {'bytes': 5, 'git_blob_sha1': hashlib.sha1(b'blob 5\0hello').hexdigest()}
            self.assertTrue(matches_source(path, expected))
            path.write_bytes(b'jello')
            self.assertFalse(matches_source(path, expected))


class MeasurementValidationTests(unittest.TestCase):
    def test_counts_are_exact_beyond_double_precision(self):
        expected = [(2**60 + 1,)]
        check_result(expected, expected)
        for value in [2**60, float(2**60 + 1), True]:
            with self.assertRaises(ValueError):
                check_result([(value,)], expected)

    def test_float_tolerance_does_not_accept_real_mismatch(self):
        check_result([(1.0 + 1e-12, None)], [(1.0, None)])
        for actual in [[(1.01, None)], [(1.0, 0)], []]:
            with self.assertRaises(ValueError):
                check_result(actual, [(1.0, None)])

    def test_sql_requires_resolved_parameters(self):
        with self.assertRaises(ValueError):
            render('SELECT * FROM {{table}} WHERE ts >= {{start}}', 'can.signals', {})
        self.assertEqual(render('SELECT * FROM {{table}}', 'can.signals', {}),
                         'SELECT * FROM can.signals')

    def test_p95_uses_all_samples(self):
        self.assertAlmostEqual(percentile([1, 1, 1, 1, 1, 1, 11], .95), 8.0)


if __name__ == '__main__':
    unittest.main()
