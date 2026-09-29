import unittest

import regrade_v4 as v4


class V4BenchmarkTests(unittest.TestCase):
    def test_only_identity_references_change(self):
        original = v4.runner.load_cases()
        revised, changed = v4.revised_cases()
        self.assertEqual(len(changed), 18)
        for before, after in zip(original, revised):
            self.assertEqual(before['question'], after['question'])
            self.assertEqual(before['id'], after['id'])
            if before['id'] in changed:
                self.assertTrue(after['reference'].startswith(before['reference'] + ' ('))
            else:
                self.assertEqual(before['reference'], after['reference'])
        self.assertIn('신예린 (User:15)', next(c['reference'] for c in revised if c['id'] == 'v3-023'))


if __name__ == '__main__':
    unittest.main()
