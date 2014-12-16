# -*- coding: utf-8 -*-

# Copyright (c) 2013-2014, GEM Foundation.
#
# OpenQuake Risklib is free software: you can redistribute it and/or
# modify it under the terms of the GNU Affero General Public License
# as published by the Free Software Foundation, either version 3 of
# the License, or (at your option) any later version.
#
# OpenQuake Risklib is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with OpenQuake Risklib. If not, see
# <http://www.gnu.org/licenses/>.


import unittest
import mock

import numpy

from openquake.risklib import workflows, scientific

aaae = numpy.testing.assert_array_almost_equal


def asset(values, deductibles=None,
          insurance_limits=None,
          retrofitting_values=None):
    return workflows.Asset('a1', 'taxonomy', 1, (0, 0), values,
                           deductibles, insurance_limits,
                           retrofitting_values)


class NormalizeTestCase(unittest.TestCase):
    loss_type = 'structural'

    def setUp(self):
        self.vf = {self.loss_type: mock.MagicMock()}
        self.poes = [0.1, 0.2]
        self.workflow = workflows.ProbabilisticEventBased(
            'PGA', 'TAXO', self.vf, 50, 1000, 20, self.poes, True)
        self.workflow.conditional_loss_poes = self.poes
        self.workflow.curves = mock.Mock(return_value=numpy.empty((3, 2, 20)))

    def test_normalize_all_trivial(self):
        poes = numpy.linspace(1, 0, 11)
        losses = numpy.zeros(11)
        curves = [[losses, poes], [losses, poes / 2]]
        exp_losses, (poes1, poes2) = self.workflow._normalize_curves(curves)

        numpy.testing.assert_allclose(exp_losses, losses)
        numpy.testing.assert_allclose(poes1, poes)
        numpy.testing.assert_allclose(poes2, poes / 2)

    def test_normalize_one_trivial(self):
        trivial = [numpy.zeros(6), numpy.linspace(1, 0, 6)]
        curve = [numpy.linspace(0., 1., 6), numpy.linspace(1., 0., 6)]
        with numpy.errstate(invalid='ignore', divide='ignore'):
            exp_losses, (poes1, poes2) = self.workflow._normalize_curves(
                [trivial, curve])

        numpy.testing.assert_allclose(exp_losses, curve[0])
        numpy.testing.assert_allclose(poes1, [numpy.nan, 0., 0., 0., 0., 0.])
        numpy.testing.assert_allclose(poes2, curve[1])


class ScenarioTestCase(unittest.TestCase):
    loss_type = 'structural'

    def test_call(self):
        vf = mock.MagicMock()
        calc = workflows.Scenario('PGA', 'TAXO', vf, True)

        assets = [asset(
            dict(structural=10),
            deductibles=dict(structural=0.1),
            insurance_limits=dict(structural=0.8))] * 4

        calc.risk_functions[self.loss_type].apply_to = mock.Mock(
            return_value=numpy.empty((4, 2)))

        (_assets, loss_ratio_matrix, aggregate_losses,
         insured_loss_matrix, insured_losses) = \
            calc(self.loss_type, assets, mock.Mock(), mock.Mock())

        self.assertEqual((4, 2), loss_ratio_matrix.shape)
        self.assertEqual((2,), aggregate_losses.shape)
        self.assertEqual((4, 2), insured_loss_matrix.shape)
        self.assertEqual((2,), insured_losses.shape)

    def test_call_no_insured(self):
        vf = mock.MagicMock()
        calc = workflows.Scenario('PGA', 'TAXO', vf, False)

        assets = [asset(dict(structural=10))] * 4
        vf = calc.risk_functions[self.loss_type]
        vf.apply_to = mock.Mock(return_value=numpy.empty((4, 2)))

        (_assets, loss_ratio_matrix, aggregate_losses,
         insured_loss_matrix, insured_losses) = (
            calc(self.loss_type, assets, mock.Mock(), mock.Mock()))

        self.assertEqual((4, 2), loss_ratio_matrix.shape)
        self.assertEqual((2,), aggregate_losses.shape)
        self.assertIsNone(insured_loss_matrix)
        self.assertIsNone(insured_losses)


class DamageTest(unittest.TestCase):
    def test_generator(self):
        with mock.patch('openquake.risklib.scientific.scenario_damage') as m:
            fragility_functions = mock.Mock()
            calc = workflows.Damage(
                'PGA', 'TAXO', dict(damage=fragility_functions))
            calc('damage', 'assets', 'hazard', None)
            self.assertEqual(m.call_count, 6)  # called 3 x 2 times


class ClassicalDamageTestCase(unittest.TestCase):
    def test(self):
        hazard_imls = [0.05, 0.2, 0.4, 0.6, 0.8, 1, 1.2, 1.4]
        fragility_functions = [
            scientific.FragilityFunctionDiscrete(
                'slight', hazard_imls,
                [0.0, 0.771, 0.95, 0.989, 0.997, 0.999, 1., 1.]),
            scientific.FragilityFunctionDiscrete(
                'moderate', hazard_imls,
                [0, 0.5, 0.861, 0.957, 0.985, 0.994, 0.997, 0.999]),
            scientific.FragilityFunctionDiscrete(
                'extreme', hazard_imls,
                [0.0, 0.231, 0.636, 0.837, 0.924, 0.962, .981, .989]),
            scientific.FragilityFunctionDiscrete(
                'complete', hazard_imls,
                [0, 0.097, 0.414, 0.661, 0.806, 0.887, 0.933, 0.959]),
            ]
        hazard_poes = numpy.array([
            0.999999999997518,
            0.077404949,
            0.015530587,
            0.004201327,
            0.001284191,
            0.000389925,
            0.000127992,
            0.000030350,
        ])
        hazard_investigation_time = 50.
        risk_investigation_time = 100.
        classicaldamage = workflows.ClassicalDamage(
            'PGA', 'W', fragility_functions,
            hazard_imls, hazard_investigation_time,
            risk_investigation_time)
        asset = workflows.Asset('a1', 'W', 1, (83.313823, 29.461172),
                                {'structural': 1.0})
        assets, poos_per_asset = classicaldamage(
            'damage', [asset], [hazard_poes])
        self.assertEqual(assets, [asset])
        aaae(poos_per_asset[0],
             [1.0415184E-09, 1.4577245E-06, 1.9585762E-03, 6.9677521E-02,
              9.2836244E-01])
