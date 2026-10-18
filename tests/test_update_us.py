import unittest

from scripts.update_us import parse_rows, release_key, source_url


def item(code, description='Item', indent='0', units=None):
    return {'htsno': code, 'description': description, 'indent': indent,
            'units': [] if units is None else units}


class UpdateUSTests(unittest.TestCase):
    def test_explicit_release_validation(self):
        self.assertEqual(source_url('2026 Rev 21'),
                         'https://www.usitc.gov/sites/default/files/tata/hts/hts_2026_revision_21_json.json')
        self.assertLess(release_key('2026 Rev 21'), release_key('2027 Basic'))
        with self.assertRaises(ValueError):
            release_key('latest')

    def test_hierarchy_units_and_chapter_reset(self):
        # Full-size payload exercises the production completeness guard.
        payload = [item('0101', 'Animals:'), item('', 'Horses:', '1'),
                   item('0101.21.00.10', '<b>Males</b>', '2', [' No. ', ' '])]
        payload.extend(item(f'02{i:08d}') for i in range(20000))
        rows = parse_rows(payload)
        self.assertEqual(rows[1]['path_desc'], 'Animals: > Horses:')
        self.assertEqual(rows[1]['description'], 'Males')
        self.assertEqual(rows[1]['unit1'], 'No.')
        self.assertIsNone(rows[1]['unit2'])
        self.assertIsNone(rows[2]['path_desc'])
        self.assertEqual(rows[1]['hs6'], '010121')

    def test_fail_closed_for_duplicate_malformed_or_partial_data(self):
        for payload in ([item('0101'), item('0101')], [item('0101')],
                        [item('01012')], [item('0101', indent='-1')],
                        [item('0101', units='kg')], {'error': 'unavailable'}):
            with self.subTest(payload=payload):
                with self.assertRaises((ValueError, TypeError)):
                    parse_rows(payload)


if __name__ == '__main__':
    unittest.main()
