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

import numpy
import logging
import operator
import collections
from functools import partial

from openquake.hazardlib.site import SiteCollection
from openquake.hazardlib.calc.hazard_curve import (
    hazard_curves_per_trt, zero_curves, zero_maps, agg_curves)
from openquake.hazardlib.calc.filters import source_site_distance_filter, \
    rupture_site_distance_filter
from openquake.risklib import scientific
from openquake.commonlib import parallel, datastore, source
from openquake.baselib.general import AccumDict, split_in_blocks

from openquake.commonlib.calculators import base, calc


HazardCurve = collections.namedtuple('HazardCurve', 'location poes')


@parallel.litetask
def classical(sources, sitecol, gsims_assoc, monitor):
    """
    :param sources:
        a non-empty sequence of sources of homogeneous tectonic region type
    :param sitecol:
        a SiteCollection instance
    :param gsims_assoc:
        associations trt_model_id -> gsims
    :param monitor:
        a monitor instance
    :returns:
        an AccumDict rlz -> curves
    """
    max_dist = monitor.oqparam.maximum_distance
    truncation_level = monitor.oqparam.truncation_level
    imtls = monitor.oqparam.imtls
    trt_model_id = sources[0].trt_model_id
    gsims = gsims_assoc[trt_model_id]
    curves_by_gsim = hazard_curves_per_trt(
        sources, sitecol, imtls, gsims, truncation_level,
        source_site_filter=source_site_distance_filter(max_dist),
        rupture_site_filter=rupture_site_distance_filter(max_dist),
        monitor=monitor)
    return {(trt_model_id, str(gsim)): curves
            for gsim, curves in zip(gsims, curves_by_gsim)}


def agg_dicts(acc, val):
    """
    Aggregate dictionaries of hazard curves by updating the accumulator
    """
    for key in val:
        acc[key] = agg_curves(acc[key], val[key])
    return acc


@base.calculators.add('classical')
class ClassicalCalculator(base.HazardCalculator):
    """
    Classical PSHA calculator
    """
    core_func = classical
    curves_by_trt_gsim = datastore.persistent_attribute('curves_by_trt_gsim')

    def execute(self):
        """
        Run in parallel `core_func(sources, sitecol, monitor)`, by
        parallelizing on the sources according to their weight and
        tectonic region type.
        """
        monitor = self.monitor(self.core_func.__name__)
        monitor.oqparam = self.oqparam
        sources = self.composite_source_model.get_sources()
        zc = zero_curves(len(self.sitecol.complete), self.oqparam.imtls)
        zerodict = AccumDict((key, zc) for key in self.rlzs_assoc)
        gsims_assoc = self.rlzs_assoc.get_gsims_by_trt_id()
        curves_by_trt_gsim = parallel.apply_reduce(
            self.core_func.__func__,
            (sources, self.sitecol, gsims_assoc, monitor),
            agg=agg_dicts, acc=zerodict,
            concurrent_tasks=self.oqparam.concurrent_tasks,
            weight=operator.attrgetter('weight'),
            key=operator.attrgetter('trt_model_id'))
        return curves_by_trt_gsim

    def post_execute(self, curves_by_trt_gsim):
        """
        Collect the hazard curves by realization and export them.

        :param curves_by_trt_gsim:
            a dictionary (trt_id, gsim) -> hazard curves
        """
        self.curves_by_trt_gsim = curves_by_trt_gsim
        oq = self.oqparam
        zc = zero_curves(len(self.sitecol.complete), oq.imtls)
        curves_by_rlz = self.rlzs_assoc.combine_curves(
            curves_by_trt_gsim, agg_curves, zc)
        rlzs = self.rlzs_assoc.realizations
        nsites = len(self.sitecol)
        if oq.individual_curves:
            for rlz, curves in curves_by_rlz.iteritems():
                self.store_curves('rlz-%d' % rlz.ordinal, curves)

        if len(rlzs) == 1:  # cannot compute statistics
            [self.mean_curves] = curves_by_rlz.values()
            return

        weights = (None if oq.number_of_logic_tree_samples
                   else [rlz.weight for rlz in rlzs])
        mean = oq.mean_hazard_curves
        if mean:
            self.mean_curves = numpy.array(zc)
            for imt in oq.imtls:
                self.mean_curves[imt] = scientific.mean_curve(
                    [curves_by_rlz[rlz][imt] for rlz in rlzs], weights)

        self.quantile = {}
        for q in oq.quantile_hazard_curves:
            self.quantile[q] = qc = numpy.array(zc)
            for imt in oq.imtls:
                curves = [curves_by_rlz[rlz][imt] for rlz in rlzs]
                qc[imt] = scientific.quantile_curve(
                    curves, q, weights).reshape((nsites, -1))

        if mean:
            self.store_curves('mean', self.mean_curves)
        for q in self.quantile:
            self.store_curves('quantile-%s' % q, self.quantile[q])

    def hazard_maps(self, curves):
        """
        Compute the hazard maps associated to the curves
        """
        n, p = len(self.sitecol), len(self.oqparam.poes)
        maps = zero_maps((n, p), self.oqparam.imtls)
        for imt in curves.dtype.fields:
            maps[imt] = calc.compute_hazard_maps(
                curves[imt], self.oqparam.imtls[imt], self.oqparam.poes)
        return maps

    def store_curves(self, dset, curves):
        """
        Store all kind of curves, optionally computing maps and uhs curves.

        :param dset: the HDF5 dataset where to store the curves
        :param curves: an array of N curves to store
        """
        if not self.persistent:  # do nothing
            return
        oq = self.oqparam
        h5 = self.datastore.hdf5
        h5['hcurves/' + dset] = curves
        if oq.hazard_maps or oq.uniform_hazard_spectra:
            # hmaps is a composite array of shape (N, P)
            hmaps = self.hazard_maps(curves)
            if oq.hazard_maps:
                h5['hmaps/' + dset] = hmaps
            if oq.uniform_hazard_spectra:
                # uhs is an array of shape (N, I, P)
                self.datastore.hdf5['uhs/' + dset] = calc.make_uhs(hmaps)


def is_effective_trt_model(result_dict, trt_model):
    """
    Returns True on tectonic region types
    which ID in contained in the result_dict.

    :param result_dict: a dictionary with keys (trt_id, gsim)
    """
    return any(trt_model.id == trt_id for trt_id, _gsim in result_dict)


@parallel.litetask
def classical_tiling(calculator, sitecol, position, tileno, monitor):
    """
    :param calculator:
        a ClassicalCalculator instance
    :param sitecol:
        the site collection of the current tile
    :param position:
        position of the current tile in the full site collection
    :param tileno:
        the tile ordinal
    :param monitor:
        a monitor instance
    :returns:
        a dictionary file name -> full path for each exported file
    """
    calculator.sitecol = sitecol
    calculator.tileno = '.%04d' % tileno
    curves_by_trt_gsim = calculator.execute()
    curves_by_trt_gsim.indices = range(position, position + len(sitecol))
    # build the correct realizations from the (reduced) logic tree
    calculator.rlzs_assoc = calculator.composite_source_model.get_rlzs_assoc(
        partial(is_effective_trt_model, curves_by_trt_gsim))
    n_levels = sum(len(imls) for imls in calculator.oqparam.imtls.itervalues())
    tup = (len(calculator.sitecol), n_levels, len(calculator.rlzs_assoc),
           len(calculator.rlzs_assoc.realizations))
    logging.info('Processed tile %d, (sites, levels, keys, rlzs)=%s',
                 tileno, tup)
    return curves_by_trt_gsim


def agg_curves_by_trt_gsim(acc, curves_by_trt_gsim):
    """
    :param acc: AccumDict (trt_id, gsim) -> N curves
    :param curves_by_trt_gsim: AccumDict (trt_id, gsim) -> T curves

    where N is the total number of sites and T the number of sites
    in the current tile. Works by side effect, by updating the accumulator.
    """
    for k in curves_by_trt_gsim:
        acc[k][curves_by_trt_gsim.indices] = curves_by_trt_gsim[k]
    return acc


@base.calculators.add('classical_tiling')
class ClassicalTilingCalculator(ClassicalCalculator):
    """
    Classical Tiling calculator
    """
    SourceProcessor = source.SourceFilter

    def execute(self):
        """
        Split the computation by tiles which are run in parallel.
        """
        monitor = self.monitor(self.core_func.__name__)
        monitor.oqparam = oq = self.oqparam
        self.tiles = split_in_blocks(
            self.sitecol, self.oqparam.concurrent_tasks or 1)
        oq.concurrent_tasks = 0
        calculator = ClassicalCalculator(
            self.oqparam, monitor, persistent=False)
        calculator.composite_source_model = self.composite_source_model
        rlzs_assoc = self.composite_source_model.get_rlzs_assoc()
        self.rlzs_assoc = calculator.rlzs_assoc = rlzs_assoc

        # parallelization
        all_args = []
        position = 0
        for (i, tile) in enumerate(self.tiles):
            all_args.append((calculator, SiteCollection(tile),
                             position, i, monitor))
            position += len(tile)
        acc = {trt_gsim: zero_curves(len(self.sitecol), oq.imtls)
               for trt_gsim in calculator.rlzs_assoc}
        return parallel.starmap(classical_tiling, all_args).reduce(
            agg_curves_by_trt_gsim, acc)
