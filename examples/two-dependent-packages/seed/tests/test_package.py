import unittest

import greeter


class PackageTest(unittest.TestCase):
    def test_importable(self):
        self.assertTrue(greeter.__doc__)
