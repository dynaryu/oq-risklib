#  -*- coding: utf-8 -*-
#  vim: tabstop=4 shiftwidth=4 softtabstop=4

#  Copyright (c) 2014-2015, GEM Foundation

#  OpenQuake is free software: you can redistribute it and/or modify it
#  under the terms of the GNU Affero General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.

#  OpenQuake is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.

#  You should have received a copy of the GNU Affero General Public License
#  along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

import math
from nose.plugins.attrib import attr

import numpy.testing

from openquake.baselib.general import groupby
from openquake.commonlib.datastore import DataStore
from openquake.commonlib.util import max_rel_diff_index
from openquake.commonlib.tests.calculators import CalculatorTestCase
from openquake.qa_tests_data.event_based import (
    blocksize, case_1, case_2, case_4, case_5, case_6, case_7, case_12,
    case_13, case_17, case_18)
from openquake.qa_tests_data.event_based.spatial_correlation import (
    case_1 as sc1, case_2 as sc2, case_3 as sc3)


def joint_prob_of_occurrence(gmvs_site_1, gmvs_site_2, gmv, time_span,
                             num_ses, delta_gmv=0.1):
    """
    Compute the Poissonian probability of a ground shaking value to be in the
    range [``gmv`` - ``delta_gmv`` / 2, ``gmv`` + ``delta_gmv`` / 2] at two
    different locations within a given ``time_span``.

    :param gmvs_site_1, gmvs_site_2:
        Lists of ground motion values (as floats) for two different sites.
    :param gmv:
        Reference value for computing joint probability.
    :param time_span:
        `investigation_time` parameter from the calculation which produced
        these ground motion values.
    :param num_ses:
        `ses_per_logic_tree_path` parameter from the calculation which produced
        these ground motion values. In other words, the total number of
        stochastic event sets.
    :param delta_gmv:
        the interval to consider
    """
    assert len(gmvs_site_1) == len(gmvs_site_2)

    half_delta = float(delta_gmv) / 2
    gmv_close = lambda v: (gmv - half_delta <= v <= gmv + half_delta)
    count = 0
    for gmv_site_1, gmv_site_2 in zip(gmvs_site_1, gmvs_site_2):
        if gmv_close(gmv_site_1) and gmv_close(gmv_site_2):
            count += 1

    prob = 1 - math.exp(- (float(count) / (time_span * num_ses)) * time_span)
    return prob


class EventBasedTestCase(CalculatorTestCase):

    @attr('qa', 'hazard', 'event_based')
    def test_spatial_correlation(self):
        expected = {sc1: [0.99, 0.41],
                    sc2: [0.99, 0.64],
                    sc3: [0.99, 0.22]}

        for case in expected:
            self.run_calc(case.__file__, 'job.ini')
            oq = self.calc.oqparam
            self.assertEqual(list(oq.imtls), ['PGA'])
            dstore = DataStore(self.calc.datastore.calc_id)
            gmf_by_rupid = groupby(
                dstore['gmfs/col00'].value,
                lambda row: row['idx'],
                lambda rows: [row['BooreAtkinson2008']['PGA'] for row in rows])
            dstore.close()
            gmvs_site_1 = []
            gmvs_site_2 = []
            for rupid, gmf in gmf_by_rupid.iteritems():
                gmvs_site_1.append(gmf[0])
                gmvs_site_2.append(gmf[1])
            joint_prob_0_5 = joint_prob_of_occurrence(
                gmvs_site_1, gmvs_site_2, 0.5, oq.investigation_time,
                oq.ses_per_logic_tree_path)
            joint_prob_1_0 = joint_prob_of_occurrence(
                gmvs_site_1, gmvs_site_2, 1.0, oq.investigation_time,
                oq.ses_per_logic_tree_path)

            p05, p10 = expected[case]
            numpy.testing.assert_almost_equal(joint_prob_0_5, p05, decimal=1)
            numpy.testing.assert_almost_equal(joint_prob_1_0, p10, decimal=1)

    @attr('qa', 'hazard', 'event_based')
    def test_blocksize(self):
        out = self.run_calc(blocksize.__file__, 'job.ini', concurrent_tasks=4,
                            exports='csv')
        [fname] = out['gmfs', 'csv']
        self.assertEqualFiles('expected/0-ChiouYoungs2008.csv',
                              fname, sorted)

        out = self.run_calc(blocksize.__file__, 'job.ini', concurrent_tasks=8,
                            exports='csv')
        [fname] = out['gmfs', 'csv']
        self.assertEqualFiles('expected/0-ChiouYoungs2008.csv',
                              fname, sorted)

    @attr('qa', 'hazard', 'event_based')
    def test_case_1(self):
        out = self.run_calc(case_1.__file__, 'job.ini', exports='csv')

        [fname] = out['gmfs', 'csv']
        self.assertEqualFiles(
            'expected/0-SadighEtAl1997.csv', fname, sorted)

        [fname] = out['hcurves', 'csv']
        self.assertEqualFiles(
            'expected/hazard_curve-smltp_b1-gsimltp_b1.csv', fname)

    @attr('qa', 'hazard', 'event_based')
    def test_case_2(self):
        out = self.run_calc(case_2.__file__, 'job.ini', exports='csv')
        [fname] = out['gmfs', 'csv']
        self.assertEqualFiles(
            'expected/SadighEtAl1997.csv', fname, sorted)

        [fname] = out['hcurves', 'csv']
        self.assertEqualFiles(
            'expected/hazard_curve-smltp_b1-gsimltp_b1.csv', fname)

    @attr('qa', 'hazard', 'event_based')
    def test_case_2bis(self):  # oversampling
        out = self.run_calc(case_2.__file__, 'job_2.ini', exports='csv,xml')
        ltr = out['gmfs', 'csv']  # 2 realizations, 1 TRT
        self.assertEqualFiles(
            'expected/gmf-smltp_b1-gsimltp_b1-ltr_0.csv', ltr[0])
        self.assertEqualFiles(
            'expected/gmf-smltp_b1-gsimltp_b1-ltr_1.csv', ltr[1])

        ltr0 = out['gmfs', 'xml'][0]
        self.assertEqualFiles('expected/gmf-smltp_b1-gsimltp_b1-ltr_0.xml',
                              ltr0)

        ltr = out['hcurves', 'csv']
        self.assertEqualFiles(
            'expected/hc-smltp_b1-gsimltp_b1-ltr_0.csv', ltr[0])
        # NB: we are testing that the file ltr_1.csv is equal to
        # ltr_0.csv, as it should be for the hazard curves
        self.assertEqualFiles(
            'expected/hc-smltp_b1-gsimltp_b1-ltr_0.csv', ltr[1])

    @attr('qa', 'hazard', 'event_based')
    def test_case_4(self):
        out = self.run_calc(case_4.__file__, 'job.ini', exports='csv')
        [fname] = out['hcurves', 'csv']
        self.assertEqualFiles(
            'expected/hazard_curve-smltp_b1-gsimltp_b1.csv', fname)

    @attr('qa', 'hazard', 'event_based')
    def test_case_5(self):
        expected = '''\
gmf-smltp_b2-gsimltp_@_b2_1_@_@.csv
gmf-smltp_b2-gsimltp_@_b2_2_@_@.csv
gmf-smltp_b2-gsimltp_@_b2_3_@_@.csv
gmf-smltp_b2-gsimltp_@_b2_4_@_@.csv
gmf-smltp_b2-gsimltp_@_b2_5_@_@.csv
gmf-smltp_b3-gsimltp_@_@_@_b4_1.csv'''.split()
        out = self.run_calc(case_5.__file__, 'job.ini', exports='csv')
        fnames = out['gmfs', 'csv']
        for exp, got in zip(expected, fnames):
            self.assertEqualFiles('expected/%s' % exp, got, sorted)

    @attr('qa', 'hazard', 'event_based')
    def test_case_6(self):
        # 2 models x 3 GMPEs, different weights
        expected = [
            'hazard_curve-mean.csv',
            'quantile_curve-0.1.csv',
        ]
        out = self.run_calc(case_6.__file__, 'job.ini', exports='csv')
        fnames = out['hcurves', 'csv']
        for exp, got in zip(expected, fnames):
            self.assertEqualFiles('expected/%s' % exp, got)

    @attr('qa', 'hazard', 'event_based')
    def test_case_7(self):
        # 2 models x 3 GMPEs, 100 samples * 10 SES
        expected = [
            'hazard_curve-mean.csv',
            'quantile_curve-0.1.csv',
            'quantile_curve-0.9.csv',
        ]
        out = self.run_calc(case_7.__file__, 'job.ini', exports='csv')
        fnames = out['hcurves', 'csv']
        mean_eb = self.calc.mean_curves
        for exp, got in zip(expected, fnames):
            self.assertEqualFiles('expected/%s' % exp, got)
        mean_cl = self.calc.cl.mean_curves
        for imt in mean_cl.dtype.fields:
            reldiff, _index = max_rel_diff_index(
                mean_cl[imt], mean_eb[imt], min_value=0.1)
            self.assertLess(reldiff, 0.41)

    @attr('qa', 'hazard', 'event_based')
    def test_case_12(self):
        out = self.run_calc(case_12.__file__, 'job.ini', exports='csv')
        [fname] = out['hcurves', 'csv']
        self.assertEqualFiles(
            'expected/hazard_curve-smltp_b1-gsimltp_b1_b2.csv', fname)

    @attr('qa', 'hazard', 'event_based')
    def test_case_13(self):
        out = self.run_calc(case_13.__file__, 'job.ini', exports='csv')
        [fname] = out['gmfs', 'csv']
        self.assertEqualFiles('expected/0-BooreAtkinson2008.csv',
                              fname, sorted)

        [fname] = out['hcurves', 'csv']
        self.assertEqualFiles(
            'expected/hazard_curve-smltp_b1-gsimltp_b1.csv', fname)

    @attr('qa', 'hazard', 'event_based')
    def test_case_17(self):  # oversampling
        expected = [
            'hazard_curve-smltp_b1-gsimltp_@-ltr_0.csv',
            'hazard_curve-smltp_b2-gsimltp_b1-ltr_1.csv',
            'hazard_curve-smltp_b2-gsimltp_b1-ltr_2.csv',
            'hazard_curve-smltp_b2-gsimltp_b1-ltr_3.csv',
            'hazard_curve-smltp_b2-gsimltp_b1-ltr_4.csv',
        ]
        out = self.run_calc(case_17.__file__, 'job.ini', exports='csv')
        fnames = out['hcurves', 'csv']
        for exp, got in zip(expected, fnames):
            self.assertEqualFiles('expected/%s' % exp, got, sorted)

    @attr('qa', 'hazard', 'event_based')
    def test_case_18(self):  # oversampling, 3 realizations
        expected = [
            'gmf-smltp_b1-gsimltp_AB-ltr_0.csv',
            'gmf-smltp_b1-gsimltp_AB-ltr_1.csv',
            'gmf-smltp_b1-gsimltp_CF-ltr_2.csv',
        ]
        out = self.run_calc(case_18.__file__, 'job_3.ini', exports='csv')
        fnames = out['gmfs', 'csv']
        for exp, got in zip(expected, fnames):
            self.assertEqualFiles('expected/%s' % exp, got, sorted)
