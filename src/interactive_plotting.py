#!/usr/bin/env python3
    
from sklearn.gaussian_process.kernels import *
from sklearn import neighbors
from scipy.sparse import issparse
from scipy.spatial import distance_matrix, ConvexHull

from functools import reduce
from collections import defaultdict
from itertools import product

import warnings

import numpy as np
import pandas as pd
import scanpy.api as sc

import matplotlib.cm as cm
import matplotlib

from bokeh.plotting import figure, show
from bokeh.models import ColumnDataSource, Slider, HoverTool, ColorBar, \
        Patches, Legend, CustomJS, TextInput, LabelSet
from bokeh.models.mappers import CategoricalColorMapper
from bokeh.layouts import layout, column, row, GridSpec
from bokeh.transform import linear_cmap, factor_mark, factor_cmap
from bokeh.core.enums import MarkerType
from bokeh.palettes import Set1, Set2, Set3, viridis
from bokeh.models.widgets.buttons import Button


_inter_hist_js_code="""
    // here is where original data is stored
    var x = orig.data['values'];

    x = x.sort((a, b) => a - b);
    var n_bins = parseInt(bins.value); // can be either string or int
    var bin_size = (x[x.length - 1] - x[0]) / n_bins;

    var hist = new Array(n_bins).fill().map((_, i) => { return 0; });
    var l_edges = new Array(n_bins).fill().map((_, i) => { return x[0] + bin_size * i; });
    var r_edges = new Array(n_bins).fill().map((_, i) => { return x[0] + bin_size * (i + 1); });
    var indices = new Array(n_bins).fill().map((_) => { return []; });

    // create the histogram
    for (var i = 0; i < x.length; i++) {
        for (var j = 0; j < r_edges.length; j++) {
            if (x[i] <= r_edges[j]) {
                hist[j] += 1;
                indices[j].push(i);
                break;
            }
        }
    }

    // make it a density
    var sum = hist.reduce((a, b) => a + b, 0);
    var deltas = r_edges.map((c, i) => { return c - l_edges[i]; });
    // just like in numpy
    hist = hist.map((c, i) => { return c / deltas[i] / sum; });

    source.data['hist'] = hist;
    source.data['l_edges'] = l_edges;
    source.data['r_edges'] = r_edges;
    source.data['indices'] = indices;

    source.change.emit();
"""


def _cmap_to_colors(cmap_vals):
    return list(('#' + ''.join(map(lambda val: '{:02x}'.format(val).upper(), item[:-1])) for item in cmap_vals))


def _create_mapper(adata, key):
    """
    Helper function to create CategoricalColorMapper from annotated data

    Params
    --------
        adata: AnnData
            annotated data object
        key: str
            key in `adata.obs.obs_keys()`, for which we want the colors; if no colors for given
            column are found in `adata.uns[key_colors]`, use Viridis palette

    Returns
    --------
        mapper: bokeh.models.mappers.CategoricalColorMapper
            mapper which maps valuems from `adata.obs[key]` to colors
    """
    # TODO:
    # plate colors return float
    palette = adata.uns.get(f'{key}_colors', viridis(len(adata.obs[key].unique())))
    key_col = adata.obs[key].astype('category') if adata.obs[key].dtype.name != 'category' else adata.obs[key]
    return CategoricalColorMapper(palette=palette, factors=list(map(str, key_col.cat.categories)))


def interactive_histograms(adata, keys=['n_counts', 'n_genes'],
                           bins='auto', min_bins=1, max_bins=1000,
                           tools='pan, reset, wheel_zoom, save',
                           groups=None, fill_alpha=0.4,
                           palette=None, 
                           legend_loc='top_right', display_all=True,
                           *args, **kwargs):
    """Utility function to plot count distributions\

    Uses the bokey library to create interactive histograms, which can be used
    e.g. to set filtering thresholds.

    Params
    --------
    adata: AnnData Object
        annotated data object
    keys: list, optional (default: `["n_counts", "n_genes"]`)
        keys in `adata.obs` or `adata.var` where the distibutions are stored
    bins: int, str, optional (default: `auto`)
        number of bins used for plotting or str from numpy.histogram
    min_bins: int, optional (default: `1`)
        minimum number of bins possible
    max_bins: int, optional (default: `1000`)
        maximum number of bins possible
    groups: list[str], (default: `None`)
        keys in `adata.obs.obs_keys()`, groups by all possible combinations of values, e.g. for
        3 plates and 2 time points, we would create total of 6 groups
    fill_alpha: float[0.0, 1.0], (default: `0.4`)
        alpha channel of fill color
    legend_loc: str, (default: `top_right`)
        position of the legend
    tools: str, optional (default: `"pan,reset, wheel_zoom, save"`)
        palette of interactive tools for the user
    palette: list(str), optional (default: `None`)
         list of colors; if None, `Set1[9] + Set2[8] + Set3[12]` from bokeh.palettes
    display_all: bool, optional (default: `True`)
        display the statistics for all data
    **kwargs: keyword arguments for figure
        specify e.g. `"plot_width"` to set the width of the figure.

    Returns
    --------
    None
    """


    if min_bins < 1:
        raise ValueError(f'Expected min_bins >= 1, got min_bins={min_bins}.')
    if max_bins < min_bins:
        raise ValueError(f'Expected min_bins <= max_bins, got min_bins={min_bins}, max_bins={max_bins}.')

    palette = Set1[9] + Set2[8] + Set3[12] if palette is None else palette

    # check the input
    for key in keys:
        if key not in adata.obs.keys() and \
           key not in adata.var.keys() and \
           key not in adata.var_names:
            raise ValueError(f'The key `{key}` does not exist in `adata.obs`, `adata.var` or `adata.var_names`.')

    def _create_adata_groups():
        if groups is None:
            return [adata], [('all',)]

        combs = list(product(*[set(adata.obs[g]) for g in groups]))
        adatas= [adata[reduce(lambda l, r: l & r,
                              (adata.obs[k] == v for k, v in zip(groups, vals)), True)]
                 for vals in combs] + [adata]

        if display_all:
            combs += [('all',)]
            adatas += [adata]

        return adatas, combs

    # group_v_combs contains the value combinations
    # used for grupping
    ad_gs = _create_adata_groups()
    
    cols = []
    for key in keys:
        # create histogram
        callbacks = []
        fig = figure(*args, tools=tools, **kwargs)
        slider = Slider(start=min_bins, end=max_bins, value=0, step=1,
                        title='Bins')

        plots = []
        for j, (ad, group_vs) in enumerate(filter(lambda ad_g: ad_g[0].n_obs > 0, zip(*ad_gs))):

            if key in ad.obs.keys():
                orig = ad.obs[key]
                hist, edges = np.histogram(orig, density=True, bins=bins)
            elif key in ad.var.keys():
                orig = ad.var[key]
                hist, edges = np.histogram(orig, density=True, bins=bins)
            else:
                orig = ad[:, key].X
                hist, edges = np.histogram(orig, density=True, bins=bins)

            slider.value = len(hist)

            # original data, used for recalculation of histogram in JS code
            orig = ColumnDataSource(data=dict(values=orig))
            # data that we update in JS code
            source = ColumnDataSource(data=dict(hist=hist, l_edges=edges[:-1], r_edges=edges[1:]))

            legend = ', '.join(': '.join(map(str, gv)) for gv in zip(groups, group_vs)) \
                    if groups is not None else 'all'
            # create figure
            p = fig.quad(source=source, top='hist', bottom=0,
                         left='l_edges', right='r_edges',
                         fill_color=palette[j], legend=legend,
                         muted_alpha=0,
                         line_color="#555555", fill_alpha=fill_alpha)

            # create callback and slider
            callback = CustomJS(args=dict(source=source, orig=orig), code=_inter_hist_js_code)
            callback.args['bins'] = slider
            callbacks.append(callback)

            # add the current plot so that we can set it
            # visible/invisible in JS code
            plots.append(p)

        # slider now updates all values
        slider.js_on_change('value', *callbacks)

        button = Button(label='Toggle', button_type='primary')
        button.callback = CustomJS(
            args={'plots': plots},
            code='''
                for (var i = 0; i < plots.length; i++) {
                    plots[i].muted = !plots[i].muted;
                }
            '''
        )

        fig.legend.location = legend_loc
        fig.xaxis.axis_label = key
        fig.yaxis.axis_label = 'normalized frequency'
        fig.plot_width = kwargs.get('plot_width', 400)
        fig.plot_height = kwargs.get('plot_height', 400)

        fig.legend.click_policy = 'mute'
        cols.append(column(slider, button, fig))

    # transform list of pairs of figures and sliders into list of lists, where
    # each sublist has length <= 2
    # note that bokeh does not like np.arrays
    grid = list(map(list, np.array_split(cols, np.ceil(len(cols) / 2))))

    show(layout(children=grid, sizing_mode='fixed', ncols=2))


def hist_thresh(adata, key, categories=dict(), bases=['umap'], components=[1, 2],
                bins='auto', palette=None):
    """Histogram with the option to subset categories based on the binned values.

    Params
    --------
    adata: AnnData
        annotated data object
    key: str
        key in `adata.obs_keys()` where the distibution is stored
    categories: dict, optional (default: `{}`)
        dictionary with keys corresponding to group names and values as starting boundaries [start_min, start_max]
    bases: list, optional (default: `['umap']`)
        bases in `adata.obsm_keys()` to visualize
    components: list(int), list(list(int)), optional (default: `[1, 2]`)
        components to use for each basis
    bins: int, str, optional (default: `auto`)
        number of bins used for initial plotting or str from numpy.histogram
    palette: list(str), optional (default: `None`)
         palette to use when coloring categories, if None, Set3[12] from bokeh.palettes is used

    Returns
    --------
    None
    """

    if not isinstance(components[0], list):
        components = [components]

    if len(components) != len(bases):
        assert len(bases) % len(components) == 0 and len(bases) >= len(components)
        components = components * (len(bases) // len(components))

    if not isinstance(components, np.ndarray):
        components = np.asarray(components)

    if not isinstance(bases, list):
        bases = [bases]

    palette = Set3[12] if palette is None else palette

    # this does not speed these kinds of glyphs
    hist_fig = figure()  # output_backend='webgl')
    hist_fig.xaxis.axis_label = key
    hist_fig.yaxis.axis_label = 'normalized frequency'
    hist, edges = np.histogram(adata.obs[key], density=True, bins=bins)
    
    source = ColumnDataSource(data=dict(hist=hist, l_edges=edges[:-1], r_edges=edges[1:],
                              category=['default'] * len(hist), indices=[[]] * len(hist)))

    df = pd.concat([pd.DataFrame(adata.obsm[f'X_{basis}'][:, comp - (basis != 'diffmap')], columns=[f'x_{basis}', f'y_{basis}'])
                    for basis, comp in zip(bases, components)], axis=1)
    df['values'] = list(adata.obs[key])
    df['category'] = 'default'
    df['visible_category'] = 'default'
    df['cat_stack'] = [['default']] * len(df)

    orig = ColumnDataSource(df)
    color = dict(field='category', transform=CategoricalColorMapper(palette=palette, factors=list(categories.keys())))
    hist_fig.quad(source=source, top='hist', bottom=0,
                  left='l_edges', right='r_edges', color=color,
                  line_color="#555555", legend='category')

    emb_figs = []
    for basis, comp in zip(bases, components):
        # this does not speed these kinds of glyphs
        fig = figure(title=basis)  # , output_backend='webgl')
        fig.xaxis.axis_label = f'{basis}_{comp[0]}'
        fig.yaxis.axis_label = f'{basis}_{comp[1]}'
        fig.scatter(f'x_{basis}', f'y_{basis}', source=orig, size=10, color=color, legend='category')
        emb_figs.append(fig)

    # create callback and slider

    inputs, category_cbs = [], []
    code_start, code_mid, code_thresh = [], [], []
    args = {'source': source, 'orig': orig}

    for col, cat_item in zip(palette, categories.items()):
        cat, (start, end) = cat_item
        inp_min = TextInput(name='test', value=f'{start}', title=f'{cat}/min')
        inp_max = TextInput(name='test', value=f'{end}', title=f'{cat}/max')

        code_start.append(f'''
            var min_{cat} = parseInt(inp_min_{cat}.value);
            var max_{cat} = parseInt(inp_max_{cat}.value);
        ''')
        code_mid.append(f'''
            var mid_{cat} = (source.data['r_edges'][i] - source.data['l_edges'][i]) / 2;
        ''')
        code_thresh.append(f'''
            if (source.data['l_edges'][i] + mid_{cat} >= min_{cat} && source.data['r_edges'][i] - mid_{cat} <= max_{cat}) {{
                source.data['category'][i] = '{cat}';
                for (var j = 0; j < source.data['indices'][i].length; j++) {{
                    var ix = source.data['indices'][i][j];
                    orig.data['category'][ix] = '{cat}';
                }}
            }}
        ''')
        args[f'inp_min_{cat}'] = inp_min
        args[f'inp_max_{cat}'] = inp_max
        min_ds = ColumnDataSource(dict(xs=[start] * 2))
        max_ds = ColumnDataSource(dict(xs=[end] * 2))

        #min_pos_cb = CustomJS(args={'source': min_ds, 'inp': inp_min}, code=line_pos_code)
        #max_pos_cb = CustomJS(args={'source': max_ds, 'inp': inp_max}, code=line_pos_code)
        #category_cbs.append(category_cb)
        #inp_min.js_on_change('value', category_cb)
        #inp_max.js_on_change('value', category_cb)

        inputs.extend([inp_min, inp_max])

    code_thresh.append('''
        {
            source.data['category'][i] = 'default';
            for (var j = 0; j < source.data['indices'][i].length; j++) {
                var ix = source.data['indices'][i][j];
                orig.data['category'][ix] = 'default';
            }
        }
    ''')

    callback = CustomJS(args=args, code=f'''
        {';'.join(code_start)}
        for (var i = 0; i < source.data['hist'].length; i++) {{
            {';'.join(code_mid)}
            {' else '.join(code_thresh)}
        }}
        orig.change.emit();
        source.change.emit();
    ''')
    for input in inputs:
        input.js_on_change('value', callback)

    slider = Slider(start=1, end=100, value=len(hist), title='Bins')
    interactive_hist_cb = CustomJS(args={'source': source, 'orig': orig, 'bins': slider}, code=_inter_hist_js_code)
    slider.js_on_change('value', interactive_hist_cb, callback)

    show(column(row(hist_fig, column(slider, *inputs)), *emb_figs))


def smooth_expression(x, y, n_points=100, mode='gp', kernel_params=dict(), kernel_default_params=dict(),
                      kernel_expr=None, default=False, verbose=False, **opt_params):
    """Smooth out the expression of given values.

    Params
    --------
    x: list(num)
        list of features
    y: list(num)
        list of targets
    n_points: int, optional (default: `100`)
        number of points to extrapolate
    mode: str, optional (default: `'gp'`)
        which regressor to use, available (`gp`: Gaussian Process, `krr`: Kernel Ridge Regression)
    kernel_params: dict, optional (default: `dict()`)
        dictionary of kernels with their parameters, keys correspond to variable names
        which can be later combined using  `kernel_expr`. Supported kernels: `ConstantKernel`, `WhiteKernel`,
        `RBF`, `Mattern`, `RationalQuadratic`, `ExpSineSquared`, `DotProduct`, `PairWiseKernel`.
    kernel_expr: str, default (`None`)
        expression to combine kernel variables specified in `kernel_params`. Supported operators are `+`, `*`, `**`;
        example: kernel_expr=`'(a + b) ** 2'`, kernel_params=`{'a': ConstantKernel(1), 'b': DotProduct(2)}`
    kernel_default_params: dict, optional (default: `dict()`)
        default parameters for a kernel, if not found in `kernel_params`
    default: bool, optional (default: `False`)
        whether to use default kernel (RBF), if none specified and/or to use default
        parameters for kernel variables in` kernel_expr`, not found in `kernel_params`
        if not, throws an Exception
    verbose: bool, optional (default: `False`)
        be verbose
    **opt_params: dict, optional (default: `dict()`)
        keyword arguments for optimizer

    Returns
    --------
    x_test: np.array
        points for which we predict the values
    x_mean: np.array
        mean of the response
    cov: np.array (`None` for mode: `'krr'`)
        covariance matrix of the response
    """

    from sklearn.kernel_ridge import KernelRidge
    from sklearn.gaussian_process import GaussianProcessRegressor
    import operator as op
    import ast

    def _eval(node):
        if isinstance(node, ast.Num):
            return node.n

        if isinstance(node, ast.Name):
            if not default and node.id not in kernel_params:
                raise ValueError(f'Error while parsing `{kernel_expr}`: `{node.id}` is not a valid key in kernel_params. To use RBF kernel with default parameters, specify default=True.')
            params = kernel_params.get(node.id, kernel_default_params)
            kernel_type = params.pop('type', 'rbf')
            return kernels[kernel_type](**params)

        if isinstance(node, ast.BinOp):
            return operators[type(node.op)](_eval(node.left), _eval(node.right))

        if isinstance(node, ast.UnaryOp):
            return operators[type(node.op)](_eval(node.operand))

        raise TypeError(node)

    operators = {ast.Add : op.add,
                 ast.Mult: op.mul,
                 ast.Pow :op.pow}
    kernels = dict(const=ConstantKernel,
                   white=WhiteKernel,
                   rbf=RBF,
                   mat=Matern,
                   rq=RationalQuadratic,
                   esn=ExpSineSquared,
                   dp=DotProduct,
                   pw=PairwiseKernel)

    x_test = np.linspace(0, 1, n_points)[:, None]

    if mode == 'krr':
        gamma = opt_params.pop('gamma', None)

        if gamma is None:
            length_scale = kernel_default_params.get('length_scale', 0.2)
            gamma = 1 / (2 * length_scale ** 2)
            if verbose:
                print(f'Smoothing using KRR with length_scale: {length_scale}.')

        kernel = opt_params.pop('kernel', 'rbf')
        model = KernelRidge(gamma=gamma, kernel=kernel, **opt_params)
        model.fit(x, y)

        return x_test, model.predict(x_test), [None] * n_points

    if mode == 'gp':

        if kernel_expr is None:
            assert len(kernel_params) == 1
            kernel_expr, = kernel_params.keys()

        kernel = _eval(ast.parse(kernel_expr, mode='eval').body)
        alpha = opt_params.pop('alpha', None)
        if alpha is None:
            alpha = np.std(y) 

        optimizer = opt_params.pop('optimizer', None)
        opt_params['kernel'] = kernel

        model = GaussianProcessRegressor(alpha=alpha, optimizer=optimizer, **opt_params)
        model.fit(x, y)

        mean, cov = model.predict(x_test, return_cov=True)
        return x_test, mean, cov

    raise ValueError(f'Uknown type: `{type}`.')


def compute_dist(x_obs, x_theo):
    """
    Utility function to compute distance a between point cloud and curve

    Params
    --------
    x_obs: np.array
        observed data
    x_theo: np.array
        theoretical data/curve

    Returns 
    --------
    score:
        a distance measure
    """

    from fastdtw import fastdtw
    score, path = fastdtw(x_obs, x_theo, dist=2)

    return score


def shift_scale(x_obs, x_theo, fit_intercept=False):
    """Utility function to shift and scale the integrated velocities

    Params:
    --------
    x_obs: np.array
        observed data
    x_theo: np.array
        theoretical data/curve
    fit_intercept: bool, optional (default: `False`)
        whether to fit intercept for the linear regression

    Returns
    --------
    exp_pred:
        Shifted and scaled integrated velocities
    """

    # find the best possible scaling factor using simple lin reg
    # this accounts for not knowing beta
    from sklearn.linear_model import LinearRegression

    reg = LinearRegression(fit_intercept=fit_intercept)
    reg.fit(x_obs[:, None], x_theo)

    return reg.coef_, reg.intercept_


def pred_exp(x_test, y_test):
    """Predict gene expression based on velocities

    Params
    --------
    x_test: np.array
        grid of points in feature space for prediction
    y_test: np.array
        smoothed velocity values

    Returns
    --------
    y_pred: np.array
        predicted values from velocities
    """

    # integrate the velocity to get gene expression
    from scipy.integrate import simps
    n_points = X_test.shape[0]

    # define a function for the derivatife
    def integrate(t, y, x):
        return simps(y[:t], x[:t])

    # compute on a grid
    y_pred = np.array([integrate(t, y_test, X_test.flatten())
                       for t in range(1, n_points + 1)])

    return y_pred


def plot_velocity(adata, paths, genes=None, n_velocity_genes=5,
              exp_key='X', mode='gp', smooth=True, length_scale=0.2,
              color_key='louvain',
              path_key='louvain',
              differentiate=False,
              return_values=False,
              velo_key_ss='velocity',
              velo_key_dyn='velocity_dynamical',
              **kwargs):
    """
    Plotting function which shows expression level as well as velocity per gene as a function of DPT.
    Aim here is to check for the consistency of the computed velocities

    Params
    --------
    adata: AnnData
        annotated data matrix
    paths: list(list(str))
        different paths to visualize in a single plot
    n_velocity_genes: int, optional (default: 5)
        when `genes` is None, take `n_velocity_genes` from
        `adata.var['velocity_genes']`
    genes: list or None, optional (default: `None`)
        list of genes to show
    n_points: int, optional (default: `100`)
        number of points for the prediction
    smooth: bool, optional (default: `True`)
        whether to compute smoothed curves
    length_scale : float, optional (default `0.2`)
        length scale for RBF kernel
    path_key: str, optional (default: `'louvain'`)
        key in `adata.obs_keys()` where to look for groups specified in `paths` argument
    color_key: str, optional (default: `'louvain'`)
        key in `adata.obs_keys()` which is color in plot
    mode: str, optional (default: `'gp'`)
        whether to use Kernel Ridge Regression (`'krr'`) or a Gaussian Process (`'gp'`) for
        smoothing the expression values
    differentiate: bool, optional (default: `True`)
        whether to take the derivative of gene expression
        TODO: not yet implemented
    return_values: bool, optional (default: `False`)
        whether to return computed values
    exp_key: str, optional (default: `'X'`)
        key from adata.layers or just `'X'` to get gene expression values
    velo_key_ss, velo_key_dyn: str, optional (default: `'velocity'`)
        key from adata.layers to get velocity  values for the steady state (ss)
        and the dynamical (dyn) model
        TODO: not yet impleneted
    **kwargs: dict, optional
        keyword arguments for KRR or GP

    Returns
    --------
    data: pandas.DataFrame
    """

    if mode == 'krr':
        warnings.warn('KRR is experimental, consider using mode=`gp`')

    for path in paths:
        for p in path:
            assert p in adata.obs[path_key].cat.categories, f'`{p}` is not in `adata.obs[path_key]`. Possible values are: `{list(adata.obs[path_key].cat.categories)}`.'

    # check the input
    if 'dpt_pseudotime' not in adata.obs.keys():
        raise ValueError('Compute `dpt` first.')
    if velo_key_ss not in adata.layers.keys():
        pass
        # raise ValueError(f'Compute `{velo_key_ss}` first.')
    if velo_key_dyn not in adata.layers.keys():
        pass
        # raise ValueError(f'Compute `{velo_key_dyn}` first.')

    # check the genes list
    if genes is None:
        genes = adata[:, adata.var['velocity_genes']].var_names[:n_velocity_genes]
    else:
        # check whether all of those genes exist in the adata object
        genes_indicator = [gene in adata.var_names for gene in genes]
        if not all(genes_indicator):
            genes_missing = np.array(genes)[np.invert(genes_indicator)]
            print(f'Could not find the following genes: `{genes_missing}`.')
            genes = list(np.array(genes)[genes_indicator])


    mapper = _create_mapper(adata, color_key)

    figs = []
    for gene in genes:
        data = defaultdict(list)
        for path in paths:
            path_ix = np.in1d(adata.obs[path_key], path)
            ad = adata[path_ix].copy()
            if exp_key != 'X':
                gene_exp = ad[:, gene].layers[exp_key]
            else:
                gene_exp = ad.raw[:, gene].X
            # exclude dropouts
            ix = (gene_exp > 0)
            dpt = ad.obs['dpt_pseudotime']

            # velo_exp_ss = ad[:, gene].layers[velo_key_ss]
            # velo_exp_dyn = ad[:, gene].layers[velo_key_dyn]

            if issparse(gene_exp):
                gene_exp = gene_exp.A
            # if issparse(velo_exp_ss): velo_exp_ss = velo_exp_ss.A
            # if issparse(velo_exp_dyn): velo_exp_dyn = velo_exp_dyn.A
            gene_exp = gene_exp.flatten()
            data['expr'].append(gene_exp)
            # data['velo_expr_ss'].append(velo_exp_ss)
            # data['velo_expr_dyn'].append(velo_exp_dyn)

            # scale the steady state velocities
            # scaling_factor, _ = shift_scale(velo_exp_ss, velo_exp_dyn, fit_intercept=False)
            # velo_exp_ss = scaling_factor * velo_exp_ss

            # compute smoothed values from expression
            data['dpt'].append(list(dpt))
            data[color_key].append(list(map(str, ad.obs[color_key])))
            if smooth:
                assert all(gene_exp[ix] > 0)
                x_test, exp_mean, exp_cov = smooth_expression(dpt[ix, None], gene_exp[ix], mode=mode,
                    n_points=len(dpt), kernel_params=dict(k=dict(length_scale=length_scale)), **kwargs)
                                                          
                data['x_test'].append(x_test)
                data['x_mean'].append(exp_mean)
                data['x_cov'].append(exp_cov)

            # TODO: below is not implemented
            continue

            if differentiate:
                if not smooth:
                    raise ValueError('You must smooth the data to do compute derivatives.')

                print('Taking the derivative of gene expression...')
                spacing = x_test[1, 0] - x_test[0, 0]
                gene_grad = np.gradient(exp_mean, spacing)
                data['gene_grad'].append(gene_grad)

                # compute goodness-of-velocities measure
                x_obs_ss = np.concatenate((dpt[:, None], velo_exp_ss[:, None]), axis=1)
                x_obs_dyn = np.concatenate((dpt[:, None], velo_exp_dyn[:, None]), axis=1)
                x_theo = np.concatenate((x_test, gene_grad[:, None]), axis=1)

                weights = 1/np.sqrt(np.diag(exp_cov))
                weights = weights/sum(weights)

                score_ss = compute_dist(x_obs_ss, x_theo, weights)
                score_dyn = compute_dist(x_obs_dyn, x_theo, weights)

                data['score_ss'].append(score_ss)
                data['score_dyn'].append(score_dyn)

                print('ss_score = {:2.2e}\ndyn_score = {:2.2e}'.format(score_ss, score_dyn))

        dataframe = pd.DataFrame(data, index=list(map(lambda path: ', '.join(map(str, path)), paths)))
        figs.append(_create_velocity_figure(dataframe, color_key, title=gene, color_mapper=mapper))

    show(column(*figs))

    if return_values:
        return data


def _create_velocity_figure(dataframe, color_key, title, color_mapper):
    """
        Helper function which create a figure with smoothed velocities, including
        confidence intervals, if they exist.

        Params:
        --------
        dataframe: pandas.DataFrame
            dataframe containing the velocity data
        color_key: str
            column in `dataframe` that is mapped to colors
        color_mapper: bokeh.models.mappers.CategoricalColorMapper
            transformation which assings each value from dataframe[color_key] to color
        title: str
            title of the figure
        Returns:
        --------
        fig: bokeh.plotting.figure
            figure containing the plot
    """
    
    # these markers are nearly indistinguishble
    markers = [marker for marker in MarkerType if marker not in ['circle_cross', 'circle_x']]
    fig = figure(title=title)

    for i, (marker, (path, df)) in enumerate(zip(markers, dataframe.iterrows())):
        ds = dict(df)
        source = ColumnDataSource(ds)
        fig.scatter('dpt', 'expr', source=source, color={'field': color_key, 'transform': color_mapper},
                    marker=marker, size=10, legend=f'{path}', muted_alpha=0)
        fig.xaxis.axis_label = 'dpt'
        fig.yaxis.axis_label = 'expression'
        if ds.get('x_test') is not None:
            if ds.get('x_mean') is not None:
                fig.line('x_test', 'x_mean', source=source, muted_alpha=0, legend=path)
                if all(map(lambda val: val is not None, ds.get('x_cov', [None]))):
                    x_mean = ds['x_mean']
                    x_cov = ds['x_cov']
                    band_x = np.append(ds['x_test'][::-1], ds['x_test'])
                    # black magic, known only to the most illustrious of wizards
                    band_y = np.append((x_mean - np.sqrt(np.diag(x_cov)))[::-1], (x_mean + np.sqrt(np.diag(x_cov))))
                    fig.patch(band_x, band_y, alpha=0.1, line_color='black', fill_color='black',
                              legend=path, line_dash='dotdash', muted_alpha=0)

            if ds.get('x_grad') is not None:
                fig.line('x_test', 'x_grad', source=source, muted_alpha=0)


    fig.legend.click_policy = 'mute'

    return fig


def highlight_de(adata, basis='umap', components=[1, 2], n_top_de_genes=10,
                 de_keys='names, scores, pvals_adj, logfoldchanges',
                 cell_keys='', n_neighbors=5,
                 fill_alpha=0.1, show_hull=True):
    """
    Highlight differential expression.

    Params
    --------
    adata: AnnData
        annotated data object
    basis: str, optional (default: `'umap'`)
        basis used in visualization
    components: list(int), optional (default: `[1, 2]`)
        components of the basis
    n_top_genes: int, optional (default: `10`)
        number of differentially expressed genes to visualize when hovering
        over the cluster
    de_keys: list, str, optional (default: `'names, scores, pvals_ads, logfoldchanges'`)
        list or comma-seperated values of keys in `adata.uns['rank_genes_groups'].keys()`
        which are to be shown for each DE genes
    cell_keys: list, str, optional (default: '')
        keys in `adata.obs_keys()` to be shown for each cell when hovering over it
    n_neighbors: int, optional (default: `5`)
        number of neighbors for KNN classifier, which controls how the
        convex hull drawn around each cluster looks like
    fill_alpha: float, optional (default: `0.1`)
        alpha value of the cluster colors
    show_hull: bool, optional (default: `True`)
        whether to show the convex hull drawn along the cluster

    Returns
    --------
    None
    """
    #TODO: test if cell_keys='' works

    if 'rank_genes_groups' not in adata.uns_keys():
        raise ValueError('Run differential expression first.')


    if isinstance(de_keys, str):
        de_keys = list(dict.fromkeys(map(str.strip, de_keys.split(','))))
        if de_keys != ['']:
            assert all(map(lambda k: k in adata.uns['rank_genes_groups'].keys(), de_keys)), 'Not all keys are in `adata.uns[\'rank_genes_groups\']`.'
        else:
            de_keys = []

    if isinstance(cell_keys, str):
        cell_keys = list(dict.fromkeys(map(str.strip, cell_keys.split(','))))
        if cell_keys != ['']:
            assert all(map(lambda k: k in adata.obs.keys(), cell_keys)), 'Not all keys are in `adata.obs.keys()`.'
        else:
            cell_keys = []

    if f'X_{basis}' not in adata.obsm.keys():
        raise ValueError(f'Key `X_{basis}` not found in adata.obsm.')

    if not isinstance(components, np.ndarray):
        components = np.asarray(components)

    key = adata.uns['rank_genes_groups']['params']['groupby']
    if key not in cell_keys:
        cell_keys.insert(0, key)

    df = pd.DataFrame(adata.obsm[f'X_{basis}'][:, components - (basis != 'diffmap')], columns=['x', 'y'])
    for k in cell_keys:
        df[k] = list(map(str, adata.obs[k]))

    knn = neighbors.KNeighborsClassifier(n_neighbors)
    knn.fit(df[['x', 'y']], adata.obs[key])
    df['prediction'] = knn.predict(df[['x', 'y']])

    conv_hulls = df[df[key] == df['prediction']].groupby(key).apply(lambda df: df.iloc[ConvexHull(np.vstack([df['x'], df['y']]).T).vertices])

    mapper = _create_mapper(adata, key)
    categories = adata.obs[key].cat.categories
    fig = figure(tools='pan, reset, wheel_zoom, lasso_select, save')
    legend_dict = defaultdict(list)

    for k in categories:
        d = df[df[key] == k]
        data_source =  ColumnDataSource(d)
        legend_dict[k].append(fig.scatter('x', 'y', source=data_source, color={'field': key, 'transform': mapper}, size=5, muted_alpha=0))

    hover_cell = HoverTool(renderers=[r[0] for r in legend_dict.values()], tooltips=[(f'{key}', f'@{key}')] + [(f'{k}', f'@{k}') for k in cell_keys[1:]])

    c_hulls = conv_hulls.copy()
    de_possible = conv_hulls[key].isin(adata.uns['rank_genes_groups']['names'].dtype.names)
    ok_patches = []
    prev_cat = []
    for i, isin in enumerate((~de_possible, de_possible)):
        conv_hulls = c_hulls[isin]

        if len(conv_hulls) == 0:
            continue

        xs, ys, ks = zip(*conv_hulls.groupby(key).apply(lambda df: list(map(list, (df['x'], df['y'], df[key])))))
        tmp_data = defaultdict(list)
        tmp_data['xs'] = xs
        tmp_data['ys'] = ys
        tmp_data[key] = list(map(lambda k: k[0], ks))
        
        if i == 1:
            ix = list(map(lambda k: adata.uns['rank_genes_groups']['names'].dtype.names.index(k), tmp_data[key]))
            for k in de_keys:
                tmp = np.array(list(zip(*adata.uns['rank_genes_groups'][k])))[ix, :n_top_de_genes]
                for j in range(n_top_de_genes):
                    tmp_data[f'{k}_{j}'] = tmp[:, j]

        tmp_data = pd.DataFrame(tmp_data)
        for k in categories:
            d = tmp_data[tmp_data[key] == k]
            source = ColumnDataSource(d)

            patches = fig.patches('xs', 'ys', source=source, fill_alpha=fill_alpha, muted_alpha=0, hover_alpha=0.5,
                                  color={'field': key, 'transform': mapper} if (show_hull and i == 1) else None,
                                  hover_color={'field': key, 'transform': mapper} if (show_hull and i == 1) else None)
            legend_dict[k].append(patches)
            if i == 1:
                ok_patches.append(patches)

    hover_group = HoverTool(renderers=ok_patches, tooltips=[(f'{key}', f'@{key}'),
        ('groupby', adata.uns['rank_genes_groups']['params']['groupby']),
        ('reference', adata.uns['rank_genes_groups']['params']['reference']),
        ('rank', ' | '.join(de_keys))] + [(f'#{i + 1}', ' | '.join((f'@{k}_{i}' for k in de_keys))) for i in range(n_top_de_genes)]
    )
    

    fig.toolbar.active_inspect = [hover_group]
    if len(cell_keys) > 1:
        fig.add_tools(hover_group, hover_cell)
    else:
        fig.add_tools(hover_group)

    legend = Legend(items=list(legend_dict.items()), location='top_right')
    fig.add_layout(legend)
    fig.legend.click_policy = 'hide'  # hide does disable hovering, whereas 'mute' does not

    show(fig)


def multi_link(adata, bases=['umap', 'pca'], components=[1, 2], key='group', markers=None,
               distance=2, highlight_cutoff=True,
               highlight_only=None, palette=None, show_legend=False):
    """
    Display the distances of cells from currently highlighted cell.

    Params
    --------
    adata: AnnData
        annotated data object
    bases: list(str), optional (default:`['umap', 'pca']`)
        list of bases to use when plotting; note that only the first plot
        is currently now hoverable
    components: list(int), list(list(int)), optional (default: `[1, 2]`)
        list of components for each bases
    key: str, optional (default: `'group'`)
        key in `adata.obs_keys()` to color
    markers: list(str), optional (default: `None`)
        list of genes in `adata.var_names`, which are used to compute the distance;
        if None, take all the genes
    distance: int, str, optional (default: `2`)
        if `int`, then p-norm to use, if `'dpt'` for dpt
    highlight_cutoff: bool, optional (default: `True`)
        if `True`, do not color in cells whose distance is further away
        than the threshold specified by the slider
    highlight_only: 'str', optional (default: `None`)
        key in `adata.obs_keys()`, which makes highlighting work only on given clusters
    palette: matplotlib.colors.Colormap, list(str), optional (default: `None`)
        colormap to use, if None
    show_legend: bool, optional (default: `False`)
        display the legend also in the linked plot

    Returns
    --------
    None
    """

    palette = cm.RdYlBu if palette is None else palette
    if isinstance(palette, matplotlib.colors.Colormap):
        palette = _cmap_to_colors(palette(range(palette.N), 1., bytes=True))

    if not isinstance(components[0], list):
        components = [components]

    if len(components) != len(bases):
        assert len(bases) % len(components) == 0 and len(bases) >= len(components)
        components = components * (len(bases) // len(components))

    if not isinstance(components, np.ndarray):
        components = np.asarray(components)

    if highlight_only is not None:
        assert highlight_only in adata.obs_keys(), f'`{highlight_only}` is not in adata.obs_keys().'

    markers = adata.var_names if markers is None else markers
    gene_subset = np.in1d(adata.var_names, markers)

    start_ix = str(adata.uns.get('iroot', 0))
    if distance != 'dpt':
        dmat = distance_matrix(adata.X[:, gene_subset], adata.X[:, gene_subset], p=distance)
    else:
        if not all(gene_subset):
            warnings.warn('markers is not None, are you sure this is what you want when using `dpt` distance?')

        dmat = []
        ad_tmp = adata.copy()
        ad_tmp = ad_tmp[:, gene_subset]
        for i in range(ad_tmp.n_obs):
            ad_tmp.uns['iroot'] = i
            sc.tl.dpt(ad_tmp)
            dmat.append(list(ad_tmp.obs['dpt_pseudotime']))


    dmat = pd.DataFrame(dmat, columns=list(map(str, range(adata.n_obs))))
    df = pd.concat([pd.DataFrame(adata.obsm[f'X_{basis}'][:, comp - (basis != 'diffmap')], columns=[f'x{i}', f'y{i}'])
                    for i, (basis, comp) in enumerate(zip(bases, components))] + [dmat], axis=1)
    df['hl_color'] = np.nan
    df['index'] = range(len(df))
    df['hl_key'] = list(adata.obs[highlight_only]) if highlight_only is not None else 0
    df[key] = list(map(str, adata.obs[key]))

    ds = ColumnDataSource(df)
    mapper = linear_cmap(field_name='hl_color', palette=palette,
                         low=df[start_ix].min(), high=df[start_ix].max())
    static_fig_mapper = _create_mapper(adata, key)

    static_figs = []
    figs, renderers = [], []
    for i, basis in enumerate(bases):
        # linked plots
        fig = figure(tools='pan, reset, save, ' + ('zoom_in, zoom_out' if i == 0 else 'wheel_zoom'),
                     title=basis, plot_width=400, plot_height=400)
        scatter = fig.scatter(f'x{i}', f'y{i}', source=ds, line_color=mapper, color=mapper,
                              legend=('hl_key' if highlight_only is not None else key) if show_legend else None,
                              hover_color='black', size=8, line_width=8, line_alpha=0)
        figs.append(fig)
        renderers.append(scatter)
    
        # static plots
        fig = figure(title=basis, plot_width=400, plot_height=400)
        fig.scatter(f'x{i}', f'y{i}', source=ds, size=8, legend=key,
                    color={'field': key, 'transform': static_fig_mapper})
    
        static_figs.append(fig)

    fig = figs[0]

    end = dmat[~np.isinf(dmat)].max().max() if distance != 'dpt' else 1.0
    slider = Slider(start=0, end=end, value=end / 2, step=end / 1000,
            title='Distance ' +  '(dpt)' if distance == 'dpt' else f'({distance}-norm)')
    col_ds = ColumnDataSource(dict(value=[start_ix]))
    update_color_code = f'''
        source.data['hl_color'] = source.data[first].map(
            (x, i) => {{ return isNaN(x) ||
                        {'x > slider.value || ' if highlight_cutoff else ''}
                        source.data['hl_key'][first] != source.data['hl_key'][i]  ? NaN : x; }}
        );
    '''
    slider.callback = CustomJS(args={'slider': slider, 'mapper': mapper['transform'], 'source': ds, 'col': col_ds}, code=f'''
        mapper.high = slider.value;
        var first = col.data['value'];
        {update_color_code}
        source.change.emit();
    ''')

    h_tool = HoverTool(renderers=renderers, tooltips=[], show_arrow=False)
    h_tool.callback = CustomJS(args=dict(source=ds, slider=slider, col=col_ds), code=f'''
        var indices = cb_data.index['1d'].indices;
        if (indices.length == 0) {{
            source.data['hl_color'] = source.data['hl_color'];
        }} else {{
            var first = indices[0];
            source.data['hl_color'] = source.data[first];
            {update_color_code}
            col.data['value'] = first;
            col.change.emit();
        }}
        source.change.emit();
    ''')
    fig.add_tools(h_tool)

    color_bar = ColorBar(color_mapper=mapper['transform'], width=12, location=(0,0))
    fig.add_layout(color_bar, 'left')

    fig.add_tools(h_tool)
    show(column(slider, row(*static_figs), row(*figs)))


def plot_cell_indices(adata, key='group', basis='diffmap', components=[1, 2], cell_keys='',
                      legend_loc='top_right', tools='pan, reset, wheel_zoom, save'):
    """
    Plot cell indices. Useful when trying to set adata.uns['iroot'].

    Params
    --------
    adata: AnnData Object
        annotated data object
    key: str, optional (default: `'group'`)
        key in `adata.obs_keys()` to color
    basis: str, optional (default: `'diffmap'`)
        basis to use
    cell_keys: str, list(str), optional (default: `''`)
        keys to display from `adata.obs_keys()` when hovering over cell
    components: list[int], optional (default: `[1, 2]`)
        which components of the basis to use
    legend_loc: str, optional (default `'top_right'`)
        location of the legend
    tools: str, optional (default: `'pan, reset, wheel_zoom, save'`)
        tools for the plot

    Returns
    --------
    None
    """

    if key not in adata.obs:
        raise ValueError(f'{key} not found in `adata.obs`')

    if f'X_{basis}' not in adata.obsm_keys():
        raise ValueError(f'basis `X_{basis}` not found in `adata.obsm`')

    if not isinstance(components, type(np.array)):
        components = np.array(components)

    if isinstance(cell_keys, str):
        cell_keys = list(dict.fromkeys(map(str.strip, cell_keys.split(','))))
        if cell_keys != ['']:
            assert all(map(lambda k: k in adata.obs.keys(), cell_keys)), 'Not all keys are in `adata.obs.keys()`.'
        else:
            cell_keys = []

    df = pd.DataFrame(adata.obsm[f'X_{basis}'][:, components - (basis != 'diffmap')], columns=['x', 'y'])

    for k in cell_keys:
        df[k] = list(map(str, adata.obs[k]))

    df['index'] = range(len(df))
    df[key] = list(adata.obs[key])

    if hasattr(adata, 'obs_names'):
        cell_keys.insert(0, 'name')
        df['name'] = list(adata.obs_names)

    if 'index' not in cell_keys:
        cell_keys.insert(0, 'index')

    palette = adata.uns.get(f'{key}_colors', viridis(len(df[key].unique())))

    p = figure(title=f'{key}', tools=tools)
    key_col = adata.obs[key].astype('category') if adata.obs[key].dtype.name != 'category' else  adata.obs[key]
    renderers = []
    for c, color in zip(key_col.cat.categories, palette):
        data = ColumnDataSource(df[df[key] == c])
        renderers.append(p.scatter(x='x', y='y', size=10, color=color, legend=str(c), source=data))
    hover_cell = HoverTool(renderers=renderers, tooltips=[(f'{k}', f'@{k}') for k in cell_keys])

    p.legend.location = legend_loc
    p.xaxis.axis_label = f'{basis}_{components[0]}'
    p.yaxis.axis_label = f'{basis}_{components[1]}'

    source = ColumnDataSource(df)
    labels = LabelSet(x='x', y='y', text='index',
                      x_offset=4, y_offset=4,
                      level='glyph', source=source, render_mode='canvas')

    labels.visible = False
    p.add_tools(hover_cell)
    p.add_layout(labels)

    button = Button(label='Toggle Indices', button_type='primary')
    button.callback = CustomJS(args=dict(l=labels), code='l.visible = !l.visible;')

    show(column(button, p))
