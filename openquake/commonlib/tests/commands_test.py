import os
import mock
import shutil
import tempfile
import unittest

from openquake.commonlib.datastore import DataStore
from openquake.commonlib.commands.info import info
from openquake.commonlib.commands.show import show
from openquake.commonlib.commands.export import export
from openquake.commonlib.commands.reduce import reduce
from openquake.commonlib.commands.run import run
from openquake.qa_tests_data.classical import case_1
from openquake.qa_tests_data.classical_risk import case_3
from openquake.qa_tests_data.scenario import case_4
from openquake.qa_tests_data.event_based import case_5

DATADIR = os.path.join(os.path.dirname(__file__), 'data')


class Print(object):
    def __init__(self):
        self.lst = []

    def __call__(self, *args):
        self.lst.append(' '.join(map(str, args)))

    def __str__(self):
        return '\n'.join(self.lst)

    @classmethod
    def patch(cls):
        return mock.patch('__builtin__.print', cls())


class InfoTestCase(unittest.TestCase):
    EXPECTED = '''Reading the source model...
<CompositionInfo
b1, x15.xml, trt=[0]: 1 realization(s)>
See https://github.com/gem/oq-risklib/blob/master/doc/effective-realizations.rst for an explanation
<RlzsAssoc(1)
0,AkkarBommer2010: ['<0,b1,@_AkkarBommer2010_@_@_@_@_@,w=1.0>']>'''

    def test_zip(self):
        path = os.path.join(DATADIR, 'frenchbug.zip')
        with Print.patch() as p:
            info(path)
        self.assertEqual(self.EXPECTED, str(p))

    def test_zip_filtering(self):
        path = os.path.join(DATADIR, 'frenchbug.zip')
        with Print.patch() as p:
            info(path, filtersources=True)
        exp = self.EXPECTED + '''
c_matrix 232 B
max_realizations 1
n_imts 1
n_levels 29.0
n_sites 1
n_sources 1
output_weight 29.0'''
        self.assertEqual(exp, str(p))

    def test_zip_weighting(self):
        path = os.path.join(DATADIR, 'frenchbug.zip')
        with Print.patch() as p:
            info(path, weightsources=True)
        exp = self.EXPECTED + '''
c_matrix 232 B
input_weight 1722
max_realizations 1
n_imts 1
n_levels 29.0
n_sites 1
n_sources 1
output_weight 29.0'''
        self.assertEqual(exp, str(p))

    def test_data_transfer(self):
        path = os.path.join(DATADIR, 'frenchbug.zip')
        with Print.patch() as p:
            info(path, datatransfer=True)
        got = str(p)
        self.assertIn('RlzsAssoc', got)
        self.assertIn('Number of tasks to be generated: 14', got)


class RunShowExportTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """
        Build a datastore instance to show what it is inside
        """
        job_ini = os.path.join(os.path.dirname(case_1.__file__), 'job.ini')
        with Print.patch() as cls.p:
            cls.datastore = run(job_ini).datastore

    def test_run_calc(self):
        self.assertIn('See the output with hdfview', str(self.p))

    def test_show_calc(self):
        with Print.patch() as p:
            show(self.datastore.calc_id)
        self.assertIn('sitemesh', str(p))

        with Print.patch() as p:
            show(self.datastore.calc_id, 'sitemesh')
        self.assertEqual(str(p), '[(0.0, 0.0)]')

    def test_export_calc(self):
        with Print.patch() as p:
            export(self.datastore.calc_id, 'sitemesh', export_dir='/tmp')
        self.assertIn("['/tmp/sitemesh.csv']", str(p))


class ReduceTestCase(unittest.TestCase):
    TESTDIR = os.path.dirname(case_3.__file__)

    def test_exposure(self):
        tempdir = tempfile.mkdtemp()
        dest = os.path.join(tempdir, 'exposure_model.xml')
        shutil.copy(os.path.join(self.TESTDIR, 'exposure_model.xml'), dest)
        with Print.patch() as p:
            reduce(dest, 0.5)
        self.assertIn('Extracted 182 nodes out of 375', str(p))
        shutil.rmtree(tempdir)

    def test_source_model(self):
        tempdir = tempfile.mkdtemp()
        dest = os.path.join(tempdir, 'source_model.xml')
        shutil.copy(os.path.join(self.TESTDIR, 'source_model.xml'), tempdir)
        with Print.patch() as p:
            reduce(dest, 0.5)
        self.assertIn('Extracted 196 nodes out of 398', str(p))
        shutil.rmtree(tempdir)

    def test_site_model(self):
        testdir = os.path.dirname(case_4.__file__)
        tempdir = tempfile.mkdtemp()
        dest = os.path.join(tempdir, 'site_model.xml')
        shutil.copy(os.path.join(testdir, 'site_model.xml'), tempdir)
        with Print.patch() as p:
            reduce(dest, 0.5)
        self.assertIn('Extracted 2 nodes out of 3', str(p))
        shutil.rmtree(tempdir)

    def test_sites_csv(self):
        testdir = os.path.dirname(case_5.__file__)
        tempdir = tempfile.mkdtemp()
        dest = os.path.join(tempdir, 'sites.csv')
        shutil.copy(os.path.join(testdir, 'sites.csv'), tempdir)
        with Print.patch() as p:
            reduce(dest, 0.5)
        self.assertIn('Extracted 50 lines out of 100', str(p))
        shutil.rmtree(tempdir)
