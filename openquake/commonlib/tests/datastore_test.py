import unittest
import numpy
from openquake.commonlib.datastore import DataStore, view


@view.add('key1_upper')
def view_key1_upper(key, dstore):
    return dstore['key1'].upper()


class DataStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.dstore = DataStore()

    def tearDown(self):
        self.dstore.clear()

    def test_pik(self):
        # store pickleable Python objects
        self.dstore['key1'] = 'value1'
        self.assertEqual(len(self.dstore), 1)
        self.dstore['key2'] = 'value2'
        self.assertEqual(list(self.dstore), ['key1', 'key2'])
        del self.dstore['key2']
        self.assertEqual(list(self.dstore), ['key1'])
        self.assertEqual(self.dstore['key1'], 'value1')

        # test a datastore view
        self.assertEqual(view('key1_upper', self.dstore), 'VALUE1')

    def test_hdf5(self):
        # optional test, run only if h5py is available
        try:
            import h5py
        except ImportError:
            raise unittest.SkipTest

        # store numpy arrays as hdf5 files
        self.assertEqual(len(self.dstore), 0)
        self.dstore['/key1'] = value1 = numpy.array(['a', 'b'])
        self.dstore['/key2'] = numpy.array([1, 2])
        self.assertEqual(list(self.dstore), ['key1', 'key2'])
        del self.dstore['/key2']
        self.assertEqual(list(self.dstore), ['key1'])
        numpy.testing.assert_equal(self.dstore['key1'], value1)

        self.assertGreater(self.dstore.getsize('key1'), 0)
        self.assertGreater(self.dstore.getsize(), 0)

        # test creating and populating a dset
        dset = self.dstore.hdf5.create_dataset('dset', shape=(4, 2),
                                               dtype=int)
        dset[0] = [1, 2]
        dset[3] = [4, 5]
        numpy.testing.assert_equal(
            self.dstore['dset'][:], [[1, 2], [0, 0], [0, 0], [4, 5]])

        # it is possible to store twice the same key (work around a bug)
        self.dstore['key1'] = 'value1'
