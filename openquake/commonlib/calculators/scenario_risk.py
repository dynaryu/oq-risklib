#  -*- coding: utf-8 -*-
#  vim: tabstop=4 shiftwidth=4 softtabstop=4

#  Copyright (c) 2014, GEM Foundation

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

import os
import logging

import numpy

from openquake.baselib import general
from openquake.commonlib import parallel, datastore
from openquake.commonlib.calculators import base


def lpa(assets, means, stddevs):
    """Losses per asset"""
    return [(a.id, m, s) for a, m, s in zip(assets, means, stddevs)]


@parallel.litetask
def scenario_risk(riskinputs, riskmodel, rlzs_assoc, monitor):
    """
    Core function for a scenario computation.

    :param riskinputs:
        a list of :class:`openquake.risklib.riskinput.RiskInput` objects
    :param riskmodel:
        a :class:`openquake.risklib.riskinput.RiskModel` instance
    :param rlzs_assoc:
        a class:`openquake.commonlib.source.RlzsAssoc` instance
    :param monitor:
        :class:`openquake.commonlib.parallel.PerformanceMonitor` instance
    :returns:
        a dictionary (key_type, loss_type) -> losses where the `key_type` can
        be "agg" (for the aggregate losses) or "ins" (for the insured losses).
    """
    logging.info('Process %d, considering %d risk input(s) of weight %d',
                 os.getpid(), len(riskinputs),
                 sum(ri.weight for ri in riskinputs))
    result = general.AccumDict({rlz.ordinal: general.AccumDict()
                                for rlz in rlzs_assoc.realizations})
    # ordinal -> agg_type, loss_type -> losses
    for out_by_rlz in riskmodel.gen_outputs(
            riskinputs, rlzs_assoc, monitor):
        for out in out_by_rlz:
            assets = out.assets
            means = out.loss_matrix.mean(axis=1),
            stddevs = out.loss_matrix.std(ddof=1, axis=1)
            result[out.hid] += {
                ('asset-loss', out.loss_type): lpa(assets, means, stddevs)}
            result[out.hid] += {('agg', out.loss_type): out.aggregate_losses}
            if out.insured_loss_matrix is not None:
                means = out.insured_loss_matrix.mean(axis=1),
                stddevs = out.insured_loss_matrix.std(ddof=1, axis=1)
                result[out.hid] += {
                    ('asset-ins', out.loss_type): lpa(assets, means, stddevs)}
                result[out.hid] += {('ins', out.loss_type): out.insured_losses}
    return result


@base.calculators.add('scenario_risk')
class ScenarioRiskCalculator(base.RiskCalculator):
    """
    Run a scenario risk calculation
    """
    core_func = scenario_risk
    epsilon_matrix = datastore.persistent_attribute('epsilon_matrix')
    losses_by_key = datastore.persistent_attribute('losses_by_key')
    gmf_by_trt_gsim = datastore.persistent_attribute('gmf_by_trt_gsim')
    pre_calculator = 'scenario'
    is_stochastic = True

    def pre_execute(self):
        """
        Compute the GMFs, build the epsilons, the riskinputs, and a dictionary
        with the unit of measure, used in the export phase.
        """
        if 'gmfs' in self.oqparam.inputs:
            self.pre_calculator = None
        base.RiskCalculator.pre_execute(self)

        logging.info('Building the epsilons')
        eps_dict = self.make_eps_dict(
            self.oqparam.number_of_ground_motion_fields)
        self.epsilon_matrix = numpy.array(
            [eps_dict[a['asset_ref']] for a in self.assetcol])
        self.riskinputs = self.build_riskinputs(base.get_gmfs(self), eps_dict)

    def post_execute(self, result):
        """
        Export the loss curves and the aggregated losses in CSV format
        """
        self.losses_by_key = result
