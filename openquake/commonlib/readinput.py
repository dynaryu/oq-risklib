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
import csv
import gzip
import zipfile
import logging
import tempfile
import operator
import collections
import ConfigParser

import numpy
from shapely import wkt, geometry

from openquake.hazardlib import geo, site, correlation, imt
from openquake.risklib import workflows, riskinput

from openquake.commonlib.oqvalidation import OqParam
from openquake.commonlib.node import read_nodes, LiteralNode, context
from openquake.commonlib import nrml, valid, logictree, InvalidFile, parallel
from openquake.commonlib.oqvalidation import vulnerability_files
from openquake.commonlib.riskmodels import \
    get_fragility_functions, get_vfs
from openquake.baselib.general import groupby, AccumDict
from openquake.baselib.general import writetmp

from openquake.commonlib import source, sourceconverter

# the following is quite arbitrary, it gives output weights that I like (MS)
NORMALIZATION_FACTOR = 1E-2


class DuplicatedPoint(Exception):
    """
    Raised when reading a CSV file with duplicated (lon, lat) pairs
    """


def collect_files(dirpath, cond=lambda fullname: True):
    """
    Recursively collect the files contained inside dirpath.

    :param dirpath: path to a readable directory
    :param cond: condition on the path to collect the file
    """
    files = []
    for fname in os.listdir(dirpath):
        fullname = os.path.join(dirpath, fname)
        if os.path.isdir(fullname):  # navigate inside
            files.extend(collect_files(fullname))
        else:  # collect files
            if cond(fullname):
                files.append(fullname)
    return files


def extract_from_zip(path, candidates):
    """
    Given a zip archive and a function to detect the presence of a given
    filename, unzip the archive into a temporary directory and return the
    full path of the file. Raise an IOError if the file cannot be found
    within the archive.

    :param path: pathname of the archive
    :param candidates: list of names to search for
    """
    temp_dir = tempfile.mkdtemp()
    with zipfile.ZipFile(path) as archive:
        archive.extractall(temp_dir)
    return [f for f in collect_files(temp_dir)
            if os.path.basename(f) in candidates]


def get_params(job_inis):
    """
    Parse one or more INI-style config files.

    :param job_inis:
        List of configuration files (or list containing a single zip archive)
    :returns:
        A dictionary of parameters
    """
    if len(job_inis) == 1 and job_inis[0].endswith('.zip'):
        job_inis = extract_from_zip(
            job_inis[0], ['job_hazard.ini', 'job_haz.ini',
                          'job.ini', 'job_risk.ini'])

    not_found = [ini for ini in job_inis if not os.path.exists(ini)]
    if len(not_found) == len(job_inis):  # nothing was found
        raise IOError('File not found: %s' % not_found[0])

    cp = ConfigParser.ConfigParser()
    cp.read(job_inis)

    # drectory containing the config files we're parsing
    base_path = os.path.dirname(os.path.abspath(job_inis[0]))
    params = dict(base_path=base_path, inputs={'job_ini': ','.join(job_inis)})

    for sect in cp.sections():
        for key, value in cp.items(sect):
            if key.endswith(('_file', '_csv')):
                input_type, _ext = key.rsplit('_', 1)
                path = value if os.path.isabs(value) else os.path.join(
                    base_path, value)
                params['inputs'][input_type] = possibly_gunzip(path)
            else:
                params[key] = value

    # populate the 'source' list
    smlt = params['inputs'].get('source_model_logic_tree')
    if smlt:
        params['inputs']['source'] = [
            os.path.join(base_path, src_path)
            for src_path in source._collect_source_model_paths(smlt)]

    return params


def get_oqparam(job_ini, pkg=None, calculators=None):
    """
    Parse a dictionary of parameters from one or more INI-style config file.

    :param job_ini:
        Path to configuration file/archive or dictionary of parameters
    :param pkg:
        Python package where to find the configuration file (optional)
    :param calculators:
        Sequence of calculator names (optional) used to restrict the
        valid choices for `calculation_mode`
    :returns:
        An :class:`openquake.commonlib.oqvalidation.OqParam` instance
        containing the validate and casted parameters/values parsed from
        the job.ini file as well as a subdictionary 'inputs' containing
        absolute paths to all of the files referenced in the job.ini, keyed by
        the parameter name.
    """
    # UGLY: this is here to avoid circular imports
    from openquake.commonlib.calculators import base

    OqParam.calculation_mode.validator.choices = tuple(
        calculators or base.calculators)

    if isinstance(job_ini, dict):
        oqparam = OqParam(**job_ini)
    else:
        basedir = os.path.dirname(pkg.__file__) if pkg else ''
        inis = [os.path.join(basedir, ini) for ini in job_ini.split(',')]
        oqparam = OqParam(**get_params(inis))

    oqparam.validate()
    return oqparam


def get_mesh(oqparam):
    """
    Extract the mesh of points to compute from the sites,
    the sites_csv, or the region.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    """
    if oqparam.sites:
        lons, lats = zip(*oqparam.sites)
        return geo.Mesh(numpy.array(lons), numpy.array(lats))
    elif 'sites' in oqparam.inputs:
        csv_data = open(oqparam.inputs['sites'], 'U').read()
        coords = valid.coordinates(
            csv_data.strip().replace(',', ' ').replace('\n', ','))
        lons, lats = zip(*coords)
        return geo.Mesh(numpy.array(lons), numpy.array(lats))
    elif oqparam.region:
        # close the linear polygon ring by appending the first
        # point to the end
        firstpoint = geo.Point(*oqparam.region[0])
        points = [geo.Point(*xy) for xy in oqparam.region] + [firstpoint]
        try:
            return geo.Polygon(points).discretize(oqparam.region_grid_spacing)
        except:
            raise ValueError(
                'Could not discretize region %(region)s with grid spacing '
                '%(region_grid_spacing)s' % vars(oqparam))
    elif 'site_model' in oqparam.inputs:
        coords = [(param.lon, param.lat) for param in get_site_model(oqparam)]
        lons, lats = zip(*coords)
        return geo.Mesh(numpy.array(lons), numpy.array(lats))
    elif 'exposure' in oqparam.inputs:
        raise RuntimeError('You can extract the site collection from the '
                           'exposure with get_sitecol_assets')


def get_site_model(oqparam):
    """
    Convert the NRML file into an iterator over 6-tuple of the form
    (z1pt0, z2pt5, measured, vs30, lon, lat)

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    """
    for node in read_nodes(oqparam.inputs['site_model'],
                           lambda el: el.tag.endswith('site'),
                           source.nodefactory['siteModel']):
        yield ~node


def get_site_collection(oqparam, mesh=None, site_ids=None,
                        site_model_params=None):
    """
    Returns a SiteCollection instance by looking at the points and the
    site model defined by the configuration parameters.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param mesh:
        a mesh of hazardlib points; if None the mesh is
        determined by invoking get_mesh
    :param site_ids:
        a list of integers to identify the points; if None, a
        range(1, len(points) + 1) is used
    :param site_model_params:
        object with a method .get_closest returning the closest site
        model parameters
    """
    mesh = mesh or get_mesh(oqparam)
    site_ids = site_ids or range(1, len(mesh) + 1)
    if oqparam.inputs.get('site_model'):
        if site_model_params is None:
            # read the parameters directly from their file
            site_model_params = geo.geodetic.GeographicObjects(
                get_site_model(oqparam))
        sitecol = []
        for i, pt in zip(site_ids, mesh):
            param = site_model_params.\
                get_closest(pt.longitude, pt.latitude)
            sitecol.append(
                site.Site(pt, param.vs30, param.measured,
                          param.z1pt0, param.z2pt5, param.backarc, i))
        return site.SiteCollection(sitecol)

    # else use the default site params
    return site.SiteCollection.from_points(
        mesh.lons, mesh.lats, site_ids, oqparam)


def get_gsims(oqparam):
    """
    Return a list of GSIM instances from the gsim name in the configuration
    file (defined for scenario computations).
    """
    return map(valid.gsim, oqparam.gsim.split())


def get_correl_model(oqparam):
    """
    Return a correlation object. See :mod:`openquake.hazardlib.correlation`
    for more info.
    """
    correl_name = oqparam.ground_motion_correlation_model
    if correl_name is None:  # no correlation model
        return
    correl_model_cls = getattr(correlation, '%sCorrelationModel' % correl_name)
    return correl_model_cls(**oqparam.ground_motion_correlation_params)


def get_rupture(oqparam):
    """
    Returns a hazardlib rupture by reading the `rupture_model` file.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    """
    rup_model = oqparam.inputs['rupture_model']
    rup_node, = read_nodes(rup_model, lambda el: 'Rupture' in el.tag,
                           source.nodefactory['sourceModel'])
    conv = sourceconverter.RuptureConverter(
        oqparam.rupture_mesh_spacing, oqparam.complex_fault_mesh_spacing)
    return conv.convert_node(rup_node)


def get_gsim_lt(oqparam, trts):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param trts:
        a sequence of tectonic region types as strings
    :returns:
        a GsimLogicTree instance obtained by filtering on the provided
        tectonic region types.
    """
    gsim_file = os.path.join(
        oqparam.base_path, oqparam.inputs['gsim_logic_tree'])
    return logictree.GsimLogicTree(gsim_file, trts)


def get_source_model_lt(oqparam):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :returns:
        a :class:`openquake.commonlib.logictree.SourceModelLogicTree`
        instance
    """
    fname = oqparam.inputs['source_model_logic_tree']
    content = file(fname).read()
    return logictree.SourceModelLogicTree(
        content, oqparam.base_path, fname, validate=False,
        seed=oqparam.random_seed,
        num_samples=oqparam.number_of_logic_tree_samples)


def possibly_gunzip(fname):
    """
    A file can be .gzipped to save space (this happens
    in the debian package); in that case, let's gunzip it.

    :param fname: a file name (not zipped)
    """
    is_gz = os.path.exists(fname) and fname.endswith('.gz')
    there_is_gz = not os.path.exists(fname) and os.path.exists(fname + '.gz')
    if is_gz:
        return writetmp(gzip.open(fname).read())
    elif there_is_gz:
        return writetmp(gzip.open(fname + '.gz').read())
    return fname


def get_source_models(oqparam, source_model_lt, sitecol=None, in_memory=True):
    """
    Build all the source models generated by the logic tree.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param source_model_lt:
        a :class:`openquake.commonlib.logictree.SourceModelLogicTree` instance
    :param in_memory:
        if True, keep in memory the sources
    :returns:
        an iterator over :class:`openquake.commonlib.source.SourceModel`
        tuples
    """
    trt_id = 0
    converter = sourceconverter.SourceConverter(
        oqparam.investigation_time,
        oqparam.rupture_mesh_spacing,
        oqparam.complex_fault_mesh_spacing,
        oqparam.width_of_mfd_bin,
        oqparam.area_source_discretization)

    # consider only the effective realizations
    rlzs = logictree.get_effective_rlzs(source_model_lt)
    samples_by_lt_path = source_model_lt.samples_by_lt_path()
    for i, rlz in enumerate(rlzs):
        sm = rlz.value  # name of the source model
        smpath = rlz.lt_path
        num_samples = samples_by_lt_path[smpath]
        if num_samples > 1:
            logging.warn('The source path %s was sampled %d times',
                         smpath, num_samples)
        fname = possibly_gunzip(os.path.join(oqparam.base_path, sm))
        if in_memory:
            apply_unc = source_model_lt.make_apply_uncertainties(smpath)
            try:
                trt_models = source.parse_source_model(
                    fname, converter, apply_unc)
            except ValueError as e:
                if str(e) in ('Surface does not conform with Aki & '
                              'Richards convention',
                              'Edges points are not in the right order'):
                    raise InvalidFile('''\
    %s: %s. Probably you are using an obsolete model.
    In that case you can fix the file with the command
    python -m openquake.engine.tools.correct_complex_sources %s
    ''' % (fname, e, fname))
                else:
                    raise
        else:  # just collect the TRT models
            smodel = read_nodes(fname, lambda el: 'sourceModel' in el.tag,
                                source.nodefactory['sourceModel']).next()
            trt_models = source.TrtModel.collect(smodel)
        trts = [mod.trt for mod in trt_models]
        source_model_lt.tectonic_region_types.update(trts)

        if oqparam.inputs.get('gsim_logic_tree'):  # check TRTs
            gsim_lt = get_gsim_lt(oqparam, trts)
            for trt_model in trt_models:
                if trt_model.trt not in gsim_lt.values:
                    raise ValueError(
                        "Found in %r a tectonic region type %r inconsistent "
                        "with the ones in %r" % (sm, trt_model.trt, fname))
                trt_model.gsims = gsim_lt.values[trt_model.trt]
        else:
            gsim_lt = logictree.DummyGsimLogicTree()
        weight = rlz.weight / num_samples
        for trt_model in trt_models:
            trt_model.id = trt_id
            for src in trt_model:
                src.trt_model_id = trt_id
            trt_id += 1
        yield source.SourceModel(
            sm, weight, smpath, trt_models, gsim_lt, i, num_samples)


def filter_sources(sources, sitecol, maxdist):
    """
    """
    srcs = collections.defaultdict(list)
    for src in sources:
        sites = src.filter_sites_by_distance_to_source(maxdist, sitecol)
        if sites is not None:
            srcs[src.trt_model_id].append(src)
    return srcs


def get_filtered_source_models(oqparam, source_model_lt, sitecol,
                               in_memory=True):
    """
    Build the source models generated by the logic tree by filtering
    according to the `asset_hazard_distance`.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param source_model_lt:
        a :class:`openquake.commonlib.logictree.SourceModelLogicTree` instance
    :param sitecol:
        a SiteCollection
    :param in_memory:
        if True, keep in memory the sources
    :returns:
        an iterator over :class:`openquake.commonlib.source.SourceModel`
        tuples skipping the empty models
    """
    for source_model in get_source_models(
            oqparam, source_model_lt, in_memory=in_memory):
        trt_by_id = {}
        all_sources = []
        for trt_model in source_model.trt_models:
            trt_by_id[trt_model.id] = trt_model
            for src in trt_model:
                all_sources.append(src)
        filtered = parallel.apply_reduce(
            filter_sources, (all_sources, sitecol, oqparam.maximum_distance),
            key=operator.attrgetter('trt_model_id'))
        for trt_model_id, sources in filtered.iteritems():
            trt_by_id[trt_model_id].sources = sorted(
                sources, key=operator.attrgetter('source_id'))
            if not sources:
                logging.warn(
                    'Could not find sources close to the sites in %s '
                    'sm_lt_path=%s, maximum_distance=%s km, TRT=%s',
                    source_model.name, source_model.path,
                    oqparam.maximum_distance, trt_model.trt)
            if source_model.trt_models:
                yield source_model
                parallel.TaskManager.restart()  # hack to save memory


def get_composite_source_model(oqparam, sitecol=None, prefilter=False,
                               in_memory=True):
    """
    Build the source models by splitting the sources. If prefiltering is
    enabled, also reduce the GSIM logic trees in the underlying source models.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param sitecol:
        a :class:`openquake.hazardlib.site.SiteCollection` instance
    :param prefilter:
        if True, filter the sources before splitting them
    :param in_memory:
        if True, keep in memory the sources
    :returns:
        an iterator over :class:`openquake.commonlib.source.SourceModel`
        tuples skipping the empty models
    """
    if prefilter and not in_memory:
        raise RuntimeError('If you want to enable prefiltering, the in_memory '
                           'option must be True')
    source_model_lt = get_source_model_lt(oqparam)
    smodels = []
    trt_id = 0
    get_sm = get_filtered_source_models if prefilter else get_source_models
    for source_model in get_sm(oqparam, source_model_lt, sitecol, in_memory):
        for trt_model in source_model.trt_models:
            trt_model.id = trt_id
            trt_id += 1
            if prefilter:
                trt_model.split_sources_and_count_ruptures(
                    oqparam.area_source_discretization)
                logging.info('Processed %s', trt_model)
        smodels.append(source_model)
    csm = source.CompositeSourceModel(source_model_lt, smodels)
    return csm


def get_job_info(oqparam, source_models, sitecol):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param source_models:
        a list of :class:`openquake.commonlib.source.SourceModel` tuples
    :param sitecol:
        a :class:`openquake.hazardlib.site.SiteCollection` instance
    :returns:
        a dictionary with same parameters of the computation, in particular
        the input and output weights
    """
    # The input weight is given by the number of ruptures generated
    # by the sources; for point sources however a corrective factor
    # given by the parameter `point_source_weight` is applied
    input_weight = sum((src.weight or 0) * src_model.samples
                       for src_model in source_models
                       for trt_model in src_model.trt_models
                       for src in trt_model)

    imtls = oqparam.imtls
    n_sites = len(sitecol)

    # the imtls dictionary has values None when the levels are unknown
    # (this is a valid case for the event based hazard calculator)
    if None in imtls.values():  # there are no levels
        n_imts = len(imtls)
        n_levels = 0
    else:  # there are levels
        n_imts = len(imtls)
        n_levels = sum(len(ls) for ls in imtls.itervalues()) / float(n_imts)

    max_realizations = oqparam.number_of_logic_tree_samples or sum(
        sm.gsim_lt.get_num_paths() for sm in source_models)
    # NB: in the event based case `max_realizations` can be over-estimated,
    # if the method is called in the pre_execute phase, because
    # some tectonic region types may have no occurrencies.

    # The output weight is a pure number which is proportional to the size
    # of the expected output of the calculator. For classical and disagg
    # calculators it is given by
    # n_sites * n_realizations * n_imts * n_levels;
    # for the event based calculator is given by n_sites * n_realizations
    # * n_levels * n_imts * (n_ses * investigation_time) * NORMALIZATION_FACTOR
    output_weight = n_sites * n_imts * max_realizations
    if oqparam.calculation_mode == 'event_based':
        total_time = (oqparam.investigation_time *
                      oqparam.ses_per_logic_tree_path)
        output_weight *= total_time * NORMALIZATION_FACTOR
    else:
        output_weight *= n_levels

    logging.info('Total weight of the sources=%s', input_weight)
    logging.info('Expected output size=%s', output_weight)
    return dict(input_weight=input_weight, output_weight=output_weight,
                n_imts=n_imts, n_levels=n_levels, n_sites=n_sites,
                max_realizations=max_realizations)


def get_imts(oqparam):
    """
    Return a sorted list of IMTs as hazardlib objects
    """
    return map(imt.from_string, sorted(oqparam.imtls))


def get_risk_model(oqparam):
    """
    Return a :class:`openquake.risklib.riskinput.RiskModel` instance

   :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    """
    risk_models = {}  # (imt, taxonomy) -> workflow
    riskmodel = riskinput.RiskModel(risk_models)

    if oqparam.calculation_mode.endswith('_damage'):
        # scenario damage calculator
        fragility_functions = get_fragility_functions(
            oqparam.inputs['fragility'],
            oqparam.continuous_fragility_discretization,
            oqparam.steps_per_interval,
        )
        riskmodel.damage_states = fragility_functions.damage_states
        for taxonomy, ffs in fragility_functions.iteritems():
            imt = ffs.imt
            risk_models[imt, taxonomy] = workflows.get_workflow(
                imt, taxonomy, oqparam, fragility_functions=dict(damage=ffs))
    elif oqparam.calculation_mode.endswith('_bcr'):
        # bcr calculators
        vfs_orig = get_vfs(oqparam.inputs, retrofitted=False).items()
        vfs_retro = get_vfs(oqparam.inputs, retrofitted=True).items()
        for (imt_taxo, vf_orig), (imt_taxo_, vf_retro) in \
                zip(vfs_orig, vfs_retro):
            assert imt_taxo == imt_taxo_  # same imt and taxonomy
            risk_models[imt_taxo] = workflows.get_workflow(
                imt_taxo[0], imt_taxo[1], oqparam,
                vulnerability_functions_orig=vf_orig,
                vulnerability_functions_retro=vf_retro)
    else:
        # classical, event based and scenario calculators
        for imt_taxo, vfs in get_vfs(oqparam.inputs).iteritems():
            risk_models[imt_taxo] = workflows.get_workflow(
                imt_taxo[0], imt_taxo[1], oqparam,
                vulnerability_functions=vfs)

    return riskmodel

# ########################### exposure ############################ #


class DuplicatedID(Exception):
    """
    Raised when two assets with the same ID are found in an exposure model
    """


def get_exposure_lazy(fname):
    """
    :param fname:
        path of the XML file containing the exposure
    :returns:
        a pair (Exposure instance, list of asset nodes)
    """
    [exposure] = nrml.read_lazy(fname, ['assets'])
    description = exposure.description
    try:
        conversions = exposure.conversions
    except NameError:
        conversions = LiteralNode('conversions',
                                  nodes=[LiteralNode('costTypes', [])])
    try:
        inslimit = conversions.insuranceLimit
    except NameError:
        inslimit = LiteralNode('insuranceLimit')
    try:
        deductible = conversions.deductible
    except NameError:
        deductible = LiteralNode('deductible')
    return Exposure(
        exposure['id'], exposure['category'],
        ~description, [ct.attrib for ct in conversions.costTypes],
        ~inslimit, ~deductible, [], set()), exposure.assets


def get_exposure(oqparam):
    """
    Read the full exposure in memory and build a list of
    :class:`openquake.risklib.workflows.Asset` instances.
    If you don't want to keep everything in memory, use
    get_exposure_lazy instead (for experts only).

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :returns:
        an :class:`Exposure` instance
    """
    out_of_region = 0
    if oqparam.region_constraint:
        region = wkt.loads(oqparam.region_constraint)
    else:
        region = None
    fname = oqparam.inputs['exposure']
    exposure, assets_node = get_exposure_lazy(fname)
    relevant_cost_types = set(vulnerability_files(oqparam.inputs)) - \
        set(['occupants'])
    asset_refs = set()
    time_event = oqparam.time_event
    ignore_missing_costs = set(oqparam.ignore_missing_costs)

    def asset_gen():
        # wrap the asset generation to get a nice error message
        with context(fname, assets_node):
            for asset in assets_node:
                yield asset

    for asset in asset_gen():
        values = {}
        deductibles = {}
        insurance_limits = {}
        retrofitting_values = {}
        with context(fname, asset):
            asset_id = asset['id']
            if asset_id in asset_refs:
                raise DuplicatedID(asset_id)
            asset_refs.add(asset_id)
            taxonomy = asset['taxonomy']
            if 'damage' in oqparam.calculation_mode:
                # calculators of 'damage' kind require the 'number'
                # if it is missing a KeyError is raised
                number = asset.attrib['number']
            else:
                # some calculators ignore the 'number' attribute;
                # if it is missing it is considered 1, since we are going
                # to multiply by it
                try:
                    number = asset['number']
                except KeyError:
                    number = 1
                else:
                    # this is needed by the classical_risk calculator
                    values['fatalities'] = number

            location = asset.location['lon'], asset.location['lat']
            if region and not geometry.Point(*location).within(region):
                out_of_region += 1
                continue
        try:
            costs = asset.costs
        except NameError:
            costs = LiteralNode('costs', [])
        with context(fname, costs):
            for cost in costs:
                cost_type = cost['type']
                if cost_type not in relevant_cost_types:
                    continue
                values[cost_type] = cost['value']
                deductibles[cost_type] = cost.attrib.get('deductible')
                insurance_limits[cost_type] = cost.attrib.get('insuranceLimit')

            # check we are not missing a cost type
            missing = relevant_cost_types - set(values)
            if missing and missing <= ignore_missing_costs:
                logging.warn(
                    'Ignoring asset %s, missing cost type(s): %s',
                    asset_id, ', '.join(missing))
                for cost_type in missing:
                    values[cost_type] = None
            elif missing:
                raise ValueError("Invalid Exposure. "
                                 "Missing cost %s for asset %s" % (
                                     missing, asset_id))

        if time_event:
            for occupancy in asset.occupancies:
                with context(fname, occupancy):
                    if occupancy['period'] == time_event:
                        values['fatalities'] = occupancy['occupants']
                        break

        area = float(asset.attrib.get('area', 1))
        ass = workflows.Asset(
            asset_id, taxonomy, number, location, values, area,
            deductibles, insurance_limits, retrofitting_values)
        exposure.assets.append(ass)
        exposure.taxonomies.add(taxonomy)
    if region:
        logging.info('Read %d assets within the region_constraint '
                     'and discarded %d assets outside the region',
                     len(exposure.assets), out_of_region)
    else:
        logging.info('Read %d assets', len(exposure.assets))

    return exposure


Exposure = collections.namedtuple(
    'Exposure', ['id', 'category', 'description', 'cost_types',
                 'insurance_limit_is_absolute',
                 'deductible_is_absolute', 'assets', 'taxonomies'])


def get_specific_assets(oqparam):
    """
    Get the assets from the parameters specific_assets or specific_assets_csv

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    """
    try:
        return set(oqparam.specific_assets)
    except AttributeError:
        if 'specific_assets' not in oqparam.inputs:
            return set()
        return set(open(oqparam.inputs['specific_assets']).read().split())


def get_sitecol_assets(oqparam, exposure):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :returns:
        two sequences of the same length: the site collection and an
        array with the assets per each site, collected by taxonomy
    """
    assets_by_loc = groupby(exposure.assets, key=lambda a: a.location)
    lons, lats = zip(*sorted(assets_by_loc))
    mesh = geo.Mesh(numpy.array(lons), numpy.array(lats))
    sitecol = get_site_collection(oqparam, mesh)
    return sitecol, numpy.array([
        assets_by_loc[site.location.longitude, site.location.latitude]
        for site in sitecol])


def get_mesh_csvdata(csvfile, imts, num_values, validvalues):
    """
    Read CSV data in the format `IMT lon lat value1 ... valueN`.

    :param csvfile:
        a file or file-like object with the CSV data
    :param imts:
        a list of intensity measure types
    :param num_values:
        dictionary with the number of expected values per IMT
    :param validvalues:
        validation function for the values
    :returns:
        the mesh of points and the data as a dictionary
        imt -> array of curves for each site
    """
    number_of_values = dict(zip(imts, num_values))
    lon_lats = {imt: set() for imt in imts}
    data = AccumDict()  # imt -> list of arrays
    check_imt = valid.Choice(*imts)
    for line, row in enumerate(csv.reader(csvfile, delimiter=' '), 1):
        try:
            imt = check_imt(row[0])
            lon_lat = valid.longitude(row[1]), valid.latitude(row[2])
            if lon_lat in lon_lats[imt]:
                raise DuplicatedPoint(lon_lat)
            lon_lats[imt].add(lon_lat)
            values = validvalues(' '.join(row[3:]))
            if len(values) != number_of_values[imt]:
                raise ValueError('Found %d values, expected %d' %
                                 (len(values), number_of_values[imt]))
        except (ValueError, DuplicatedPoint) as err:
            raise err.__class__('%s: file %s, line %d' % (err, csvfile, line))
        data += {imt: [numpy.array(values)]}
    points = lon_lats.pop(imts[0])
    for other_imt, other_points in lon_lats.iteritems():
        if points != other_points:
            raise ValueError('Inconsistent locations between %s and %s' %
                             (imts[0], other_imt))
    lons, lats = zip(*sorted(points))
    mesh = geo.Mesh(numpy.array(lons), numpy.array(lats))
    return mesh, {imt: numpy.array(lst) for imt, lst in data.iteritems()}


def get_sitecol_hcurves(oqparam):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :returns:
        the site collection and the hazard curves, by reading
        a CSV file with format `IMT lon lat poe1 ... poeN`
    """
    imts = oqparam.imtls.keys()
    num_values = map(len, oqparam.imtls.values())
    with open(oqparam.inputs['hazard_curves']) as csvfile:
        mesh, hcurves_by_imt = get_mesh_csvdata(
            csvfile, imts, num_values, valid.decreasing_probabilities)
    sitecol = get_site_collection(oqparam, mesh)
    return sitecol, hcurves_by_imt


def get_sitecol_gmfs(oqparam):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :returns:
        a dictionary IMT -> array(N, R) read from a CSV file with format
        `tag indices [gmv1 ... gmvN] * num_imts`
    """
    imts = oqparam.imtls.keys()
    num_gmfs = oqparam.number_of_ground_motion_fields
    gmf_by_imt = {imt: [] for imt in imts}
    sitecol = get_site_collection(oqparam)  # extract it from inputs['sites']
    tags = []
    fname = oqparam.inputs['gmfs']
    with open(fname) as csvfile:
        for line in csvfile:
            row = line.split(',')
            try:
                indices = map(valid.positiveint, row[1].split())
            except:
                raise InvalidFile(
                    'The second column in %s is expected to contain integer '
                    'indices, got %s instead' % (fname, row[1]))
            r_sites = (
                sitecol if not indices else
                site.FilteredSiteCollection(indices, sitecol))
            for i in range(len(imts)):
                try:
                    array = numpy.array(valid.positivefloats(row[i + 2]))
                    # NB: i + 2 because the first 2 fields are tag and indices
                except:
                    raise InvalidFile(
                        'The column #%d in %s is expected to contain positive '
                        'floats, got %s instead' % (i + 3, fname, row[i + 2]))
                gmf_by_imt[imts[i]].append(r_sites.expand(array, 0))
            tags.append(row[0])
    data = gmf_by_imt[imts[0]]
    if len(data) != num_gmfs:
        raise InvalidFile('%s contains %d rows, expected %d' % (
            fname, len(data), num_gmfs))
    if tags != sorted(tags):
        raise InvalidFile('The tags in %s are not ordered: %s' % (fname, tags))
    return sitecol, {imt: numpy.array(gmf_by_imt[imt]).T for imt in imts}


def get_mesh_hcurves(oqparam):
    """
    Read CSV data in the format `lon lat, v1-vN, w1-wN, ...`.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :returns:
        the mesh of points and the data as a dictionary
        imt -> array of curves for each site
    """
    imtls = oqparam.imtls
    lon_lats = set()
    data = AccumDict()  # imt -> list of arrays
    ncols = len(imtls) + 1  # lon_lat + curve_per_imt ...
    csvfile = oqparam.inputs['hazard_curves']
    for line, row in enumerate(csv.reader(csvfile), 1):
        try:
            if len(row) != ncols:
                raise ValueError('Expected %d columns, found %d' %
                                 ncols, len(row))
            x, y = row[0].split()
            lon_lat = valid.longitude(x), valid.latitude(y)
            if lon_lat in lon_lats:
                raise DuplicatedPoint(lon_lat)
            lon_lats.add(lon_lat)
            for i, imt in enumerate(imtls, 1):
                values = valid.decreasing_probabilities(row[i])
                if len(values) != len(imtls[imt]):
                    raise ValueError('Found %d values, expected %d' %
                                     (len(values), len(imtls([imt]))))
                data += {imt: [numpy.array(values)]}
        except (ValueError, DuplicatedPoint) as err:
            raise err.__class__('%s: file %s, line %d' % (err, csvfile, line))
    lons, lats = zip(*sorted(lon_lats))
    mesh = geo.Mesh(numpy.array(lons), numpy.array(lats))
    return mesh, {imt: numpy.array(lst) for imt, lst in data.iteritems()}
