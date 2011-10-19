"""
Gaussian-process (GP)-based optimization algorithm using Theano
"""

__authors__   = "James Bergstra"
__copyright__ = "(c) 2011, James Bergstra"
__license__   = "3-clause BSD License"
__contact__   = "github.com/jaberg/hyperopt"

import numpy
import scipy.optimize
import theano
import theano.tensor as TT
from theano_linalg import (solve, cholesky, diag, matrix_inverse, det, PSD_hint,
        trace)

from hyperopt.theano_bandit_algos import TheanoBanditAlgo

def dots(*args):
    rval = args[0]
    for a in args[1:]:
        rval = theano.tensor.dot(rval, a)
    return rval


def value(x):
    try:
        return x.get_value()
    except AttributeError:
        return x


class SquaredExponentialKernel(object):
    """

    K(x,y) = exp(-0.5 ||x-y||^2 / l^2)

    Attributes:

        log_lenscale - log(2 l^2)

    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        if self.log_lenscale.ndim!=0:
            raise TypeError('log_lenscale must be scalar', self.log_lenscale)
    def lenscale(self, thing=None):
        if thing is None:
            thing = self.log_lenscale
        return numpy.sqrt(numpy.exp(value(thing)) / 2.0)
    def set_lenscale(self, new_l):
        candidate = numpy.log(2 * (value(new_l)**2))
        if candidate < self.log_lenscale_min:
            raise ValueError('lenscale too small')
            candidate = self.log_lenscale_min
        if candidate > self.log_lenscale_max:
            raise ValueError('lenscale too large')
            candidate = self.log_lenscale_max
        self.log_lenscale.set_value(candidate)

    def __str__(self):
        l = self.lenscale()
        (low,high), = self.param_bounds()
        if low is not None:
            low = self.lenscale(low)
        if high is not None:
            high = self.lenscale(high)
        return "%s{l=%s,bounds=(%s,%s)}"%(
                    self.__class__.__name__,
                    str(l),str(low), str(high))

    @classmethod
    def alloc(cls, l=1, l_min=1e-4, l_max=1000):
        log_l = numpy.log(2*(l**2))
        log_lenscale = theano.shared(log_l)
        if l_min is None:
            log_lenscale_min = None
        else:
            log_lenscale_min = numpy.log(2*(l_min**2))
        if l_max is None:
            log_lenscale_max = None
        else:
            log_lenscale_max = numpy.log(2*(l_max**2))
        return cls(log_lenscale=log_lenscale,
                log_lenscale_min=log_lenscale_min,
                log_lenscale_max=log_lenscale_max)

    def params(self):
        return [self.log_lenscale]
    def param_bounds(self):
        return [(self.log_lenscale_min, self.log_lenscale_max)]

    def K(self, x, y):
        ll2 = TT.exp(self.log_lenscale) #2l^2
        d = ((x**2).sum(axis=1).dimshuffle(0,'x')
                + (y**2).sum(axis=1)
                - 2 * TT.dot(x, y.T))
        K = TT.exp(-d/ll2)
        return K


class ExponentialKernel(object):
    """
    K(x,y) = exp(- ||x-y|| / l)

    Attributes:

        log_lenscale - log(l)

    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        if self.log_lenscale.ndim!=0:
            raise TypeError('log_lenscale must be scalar', self.log_lenscale)
    def __str__(self):
        l = numpy.exp(self.log_lenscale.value)
        return "ExponentialKernel{l=%s}"%str(l)

    @classmethod
    def alloc(cls, l=1):
        log_l = numpy.log(l)
        log_lenscale = theano.shared(log_l)
        return cls(log_lenscale=log_lenscale)

    def params(self):
        return [self.log_lenscale]
    def param_bounds(self):
        return [(self.log_lenscale_min, self.log_lenscale_max)]

    def K(self, x, y):
        l = TT.exp(self.log_lenscale)
        d = ((x**2).sum(axis=1).dimshuffle(0,'x')
                + (y**2).sum(axis=1)
                - 2 * TT.dot(x, y.T))
        K = TT.exp(-TT.sqrt(d)/l)
        return K


class CategoryKernel(SquaredExponentialKernel):
    """
    K(x,y) is 1 if x==y else exp(-1/l)

    The idea is that it's like a SquaredExponentialKernel
    where every point is a distance of 1 from every other one, 
    except itself.

    Attributes:

        log_lenscale - 

    """
    def K(self, x, y):
        xx = x.reshape((x.shape[0],))
        yy = y.reshape((y.shape[0],))
        xx = xx.dimshuffle(0,'x') # drop cols because there should only be 1
        yy = yy.dimshuffle(0)     # drop cols because there should only be 1

        ll2 = TT.exp(self.log_lenscale) #2l^2
        d = TT.neq(xx,yy)
        K = TT.exp(-d/ll2)
        return K


class ConvexMixtureKernel(object):
    """

    Attributes:
    
        kernels -
        element_ranges - each kernel looks at these elements (default ALL)
        feature_names - 
        raw_coefs - 
        coefs - 

    """
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
    def __str__(self):
        coefs = self.coefs_f()
        ks = [str(k) for k in self.kernels]
        return 'ConvexMixtureKernel{%s}'%(','.join(['%s*%s'%(str(c),s) for c,s in zip(coefs, ks)]))
    def summary(self):
        import StringIO
        ss = StringIO.StringIO()
        coefs = self.coefs_f()
        print >> ss,  "ConvexMixtureKernel:"
        for c, k,fname in zip(coefs,self.kernels, self.feature_names):
            print >> ss,  "  %f * %s '%s'" %(c, str(k), fname)
        return ss.getvalue()
    @classmethod
    def alloc(cls, kernels, coefs=None, element_ranges=None, feature_names=None):
        if coefs is None:
            raw_coefs = theano.shared(numpy.zeros(len(kernels)))
            print "HAAACK"
            raw_coefs.get_value(borrow=True)[0] += 1 
        else:
            raise NotImplementedError()
        coefs=TT.nnet.softmax(raw_coefs.dimshuffle('x',0))[0]
        coefs_f = theano.function([], coefs)
        return cls(
                kernels=kernels,
                coefs=coefs,
                coefs_f = coefs_f, #DEBUG
                raw_coefs = raw_coefs,
                element_ranges=element_ranges,
                feature_names = feature_names,
                )

    def params(self):
        rval = [self.raw_coefs]
        for k in self.kernels:
            rval.extend(k.params())
        return rval
    def param_bounds(self):
        rval = [(self.raw_coefs_min, self.raw_coefs_max)]
        for k in self.kernels:
            rval.extend(k.param_bounds())
        return rval

    def K(self, x, y):
        # get the kernel matrix from each sub-kernel
        if self.element_ranges is None:
            Ks = [kernel.K(x,y) for kernel in  self.kernels]
        else:
            assert len(self.element_ranges) == len(self.kernels)
            Ks = [kernel.K(x[:,er[0]:er[1]],y[:,er[0]:er[1]])
                    for (kernel,er) in zip(self.kernels, self.element_ranges)]
        # stack them up
        Kstack = TT.stack(*Ks)
        # multiply by coefs
        # and sum down to one kernel
        K = TT.sum(self.coefs.dimshuffle(0,'x','x') * Kstack,
                axis=0)
        return K


class ProductKernel(object):
    """

    Attributes:
    
        kernels -
        element_ranges - each kernel looks at these elements (default ALL)
        feature_names - 
        raw_coefs - 
        coefs - 

    """
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
    def __str__(self):
        ks = [str(k) for k in self.kernels]
        return 'ProductKernel{%s}'%(','.join(['%s*%s'%(str(c),s) for c,s in zip(coefs, ks)]))
    def summary(self):
        import StringIO
        ss = StringIO.StringIO()
        print >> ss,  "ProductKernel:"
        for k,fname in zip(self.kernels, self.feature_names):
            print >> ss,  "  %s '%s'" %(str(k), fname)
        return ss.getvalue()
    @classmethod
    def alloc(cls, kernels, element_ranges=None, feature_names=None):
        return cls(
                kernels=kernels,
                element_ranges=element_ranges,
                feature_names = feature_names,
                )

    def params(self):
        rval = []
        for k in self.kernels:
            rval.extend(k.params())
        return rval
    def param_bounds(self):
        rval = []
        for k in self.kernels:
            rval.extend(k.param_bounds())
        return rval

    def K(self, x, y):
        # get the kernel matrix from each sub-kernel
        if self.element_ranges is None:
            Ks = [kernel.K(x,y) for kernel in  self.kernels]
        else:
            assert len(self.element_ranges) == len(self.kernels)
            Ks = [kernel.K(x[:,er[0]:er[1]],y[:,er[0]:er[1]])
                    for (kernel,er) in zip(self.kernels, self.element_ranges)]
        # stack them up
        Kstack = TT.stack(*Ks)
        # multiply by coefs
        # and sum down to one kernel
        K = TT.prod(Kstack, axis=0)
        return K


class GPR_math(object):
    """
    Formulae for Gaussian Process Regression

    # K - the gram matrix of training data
    # y - the vector of training data values
    # var_y - the vector of training data variances

    """
    def __init__(self, x, y, var_y, K_fn):
        self.x = x
        self.y = TT.as_tensor_variable(y)
        self.var_y = TT.as_tensor_variable(var_y)
        self.K_fn = K_fn
        self.K = K_fn(x, x)
        if N is None:
            self.N = self.y.shape[0]
        else:
            self.N = N

    def kyn(self):
        """Return tuple: K, y, var_y, N"""
        return self.K, self.y, self.var_y, self.N

    def s_nll(self):
        """ Marginal negative log likelihood of model

        :note: See RW.pdf page 37, Eq. 2.30.
        """
        K, y, var_y, N = self.kyn()
        rK = PSD_hint(K + var_y * TT.eye(N))
        nll = (0.5 * dots(y, matrix_inverse(rK), y)
                + 0.5 * TT.log(det(rK))
                + n / 2.0 * TT.log(2 * numpy.pi))
        return nll

    def s_mean(self, x):
        """Gaussian Process mean at points x"""
        K, y, var_y, N = self.kyn()
        rK = PSD_hint(K + var_y * TT.eye(N))
        alpha = TT.dot(matrix_inverse(rK), y)

        K_x = self.K_fn(self.x, x)
        y_x = TT.dot(alpha, K_x)
        return y_x

    def s_variance(self, x):
        """Gaussian Process variance at points x"""
        K, y, var_y, N = self.kyn()
        rK = PSD_hint(K + var_y * TT.eye(N))
        L = cholesky(rK)
        K_x = self.K_fn(self.x, x)
        v = solve(L, K_x)
        var_x = 1 - (v**2).sum(axis=0)
        return var_x

    def s_deg_of_freedom(self):
        """
        Degrees of freedom aka "effective number of parameters"
        of kernel smoother.

        Defined pg. 25 of Rasmussen & Williams.
        """
        K, y, var_y, N = self.kyn()
        rK = PSD_hint(K + var_y * TT.eye(N))
        dof = trace(TT.dot(K, matrix_inverse(rK)))
        return dof

class GPR_algos(object):

    def minimize_nll(self, maxiter=None):
        """
        Fit GPR kernel parameters by minimizing magininal nll.

        Returns: None

        Side effect: chooses optimal kernel parameters.
        """


        if hasattr(self, 'nll_fn'):
            nll_fn = self.nll_fn
            dnll_dparams = self.dnll_dparams
        else:
            print "COMPILING AGAIN"
            cost = self.s_nll() + 0.1 * sum([(p**2).sum() for p in self.kernel.params()])
            nll_fn = self.nll_fn = theano.function([], cost)
            dnll_dparams = self.dnll_dparams = theano.function(
                    [], TT.grad(cost, self.kernel.params()))
        #theano.printing.debugprint(nll)
        params = self.kernel.params()
        param_bounds = self.kernel.param_bounds()
        lbounds = []
        ubounds = []
        for lb,ub in param_bounds:
            lbounds.extend(numpy.asarray(value(lb)).flatten())
            ubounds.extend(numpy.asarray(value(ub)).flatten())
        bounds = numpy.asarray([lbounds, ubounds]).T

        def get_pt():
            #TODO: handle non-scalar parameters...
            rval = []
            for p in params:
                v = p.get_value().flatten()
                rval.extend(v)
            return  numpy.asarray(rval)
        def set_pt(pt):
            i = 0
            for p in params:
                shape = p.get_value(borrow=True).shape
                size = numpy.prod(shape)
                p.set_value(pt[i:i+size].reshape(shape))
                i += size
            assert i == len(pt)
            #print self.kernel.summary()

        def f(pt):
            #print 'f', pt
            set_pt(pt)
            return nll_fn()
        def df(pt):
            #print 'df', pt
            set_pt(pt)
            dparams = dnll_dparams()
            rval = []
            for dp in dparams:
                rval.extend(dp.flatten())
            rval =  numpy.asarray(rval)
            #print numpy.sqrt((rval**2).sum())
            return rval
        start_pt = get_pt()
        # WEIRD: I was using fmin_ncg here
        #        until I used a low multiplier on the sum-squared-error regularizer
        #        on the 'cost' above, which threw ncg into an inf loop!?
        #best_pt = scipy.optimize.fmin_cg(f, start_pt, df, maxiter=maxiter, epsilon=.02)
        best_pt, best_value, best_d = scipy.optimize.fmin_l_bfgs_b(f, start_pt, df, maxfun=3*maxiter, 
                #bounds=[(-10, 10)]*len(start_pt))
                bounds=bounds)
        #print 'best_value', best_value

        set_pt(best_pt)
        return best_value

    def mean(self, x):
        """
        Compute mean at points in x_new
        """
        try:
            self._mean
        except AttributeError:
            s_x = TT.matrix()
            self._mean = theano.function([s_x], self.s_mean(s_x))
        return self._mean(x)

    def variance(self, x):
        """
        Compute variance at points in x_new
        """
        try:
            self._variance
        except AttributeError:
            s_x = TT.matrix()
            self._variance = theano.function([s_x], self.s_variance(s_x))
        return self._variance(x)

    def mean_variance(self, x):
        """
        Compute mean and variance at points in x_new
        """
        try:
            self._mean_variance
        except AttributeError:
            s_x = TT.matrix()
            self._mean_variance = theano.function([s_x],
                    [self.s_mean(s_x), self.s_variance(s_x)])
        return self._mean_variance(x)

    def deg_of_freedom(self):
        try:
            self._dof_fn
        except AttributeError:
            self._dof_fn = theano.function([], self.s_deg_of_freedom())
        return self._dof_fn()



class GP_BanditAlgo(TheanoBanditAlgo):
    multiplicative_kernels = True

    def __init__(self, bandit):
        TheanoBanditAlgo.__init__(self, bandit)
        self.s_prior = IdxsValsList.fromlists(self.s_idxs, self.s_vals)
        self.x_obs_IVL = self.s_prior.new_like_self()
        self.y_obs = tensor.vector()
        self.y_obs_var = tensor.vector()

        self.params = [] # to be populated by self.sparse_gram_matrix()

        self.gprmath = GPR_math(self.x_obs_IVL,
                self.y_obs,
                self.y_obs_var,
                self.self.K_fn)

        self.nll_obs = self.gprmath.s_nll()

        if 0:
            # fit the kernel parameters by gpr.minimize_nll

            # generate half the candidates randomly,
            # and half the candidates from jobs (especially good ones?)
            candidates = s_prior.new_like_self()

            # EI is integral from -inf to thresh of the
            # Gaussian-distributed prediction GP(x)
            thresh = results.min()
            EI = .5 - .5 * tensor.erf((gpr.mean(candidates) - thresh)
                    / tensor.sqrt(gpr.variance(candidates)))

            weighted_EI = EI * tensor.exp(
                    lpdf(candidates)
                    - lpdf(candidates).max())

            g_candidate_vals = grad(weighted_EI.sum(), candidates.valslist())

            # optimize EI.sum() wrt the continuous-valued variables in candidates

    def sparse_gram_matrix(self, rv, i0, v0, i1, v1, fill_value):
        raise NotImplementedError()

    def K_fn(gprmathobj, self, x0, x1):
        # for each random variable in s_prior
        # choose a kernel to compare observations of that variable
        # and do a sparse increment of the gram matrix
        if self.multiplicative_kernels:
            fill_value = 1
        else:
            fill_value = 0
        gram_matrices = [
                self.sparse_gram_matrix(iv_prior.vals,
                    iv0.idxs, iv0.vals,
                    iv1.idxs, iv1.vals,
                    fill_value=fill_value)
                for iv_prior, iv0, iv1 in zip(s_prior, x0, x1)]

        if self.multiplicative_kernels:
            return tensor.mul(*gram_matrices)
        else:
            return tensor.add(*gram_matrices)


    def build_helpers(self, do_compile=True, mode=None):
        if do_compile:
            self._helper = theano.function(
                [n_to_draw, n_to_keep, y_thresh, yvals] + s_obs.flatten(),
                (Gsamples.take(keep_idxs).flatten()
                    + Gobs.flatten()
                    + Bobs.flatten()
                    ),
                allow_input_downcast=True,
                mode=mode,
                )

            self._prior_sampler = theano.function(
                    [n_to_draw],
                    s_prior.flatten(),
                    mode=mode)


    def theano_suggest_from_prior(self, N):
        rvals = self._prior_sampler(N)
        return IdxsValsList.fromflattened(rvals)

    def theano_suggest(self, X_IVLs, Ys, N):
        if not hasattr(self, '_prior_sampler'):
            self.build_helpers()
            assert hasattr(self, '_prior_sampler')

        if len(Ys['ok']) < self.n_startup_jobs:
            logger.info('GM_BanditAlgo warming up %i/%i'
                    % (len(Ys['ok']), self.n_startup_jobs))
            return self.theano_suggest_from_prior(N)

        ylist = numpy.asarray(sorted(Ys['ok']), dtype='float')
        y_thresh_idx = int(self.gamma * len(ylist))
        y_thresh = ylist[y_thresh_idx : y_thresh_idx + 2].mean()

        logger.info('GM_BanditAlgo splitting results at y_thresh = %f'
                % y_thresh)
        logger.info('GM_BanditAlgo keeping %i results as good'
                % y_thresh_idx)
        logger.info('GM_BanditAlgo keeping %i results as bad'
                % (len(ylist) - y_thresh_idx))
        logger.info('GM_BanditAlgo good scores: %s'
                % str(ylist[:y_thresh_idx]))

        x_all = X_IVLs['ok'].copy()
        y_all = list(Ys['ok'])

        logger.info('GM_BanditAlgo assigning bad scores to %i new jobs'
                % len(Ys['new']))
        idmap = x_all.stack(X_IVLs['new'])
        assert range(len(idmap)) == list(sorted(idmap.keys()))
        y_all.extend([y_thresh + 1 for y in Ys['new']])

        logger.info('GM_BanditAlgo drawing %i candidates'
                % self.n_EI_candidates)

        helper_rval = self._helper(self.n_EI_candidates, N,
            y_thresh, y_all, *x_all.flatten())

        keep_flat = helper_rval[:2 * len(x_all)]
        Gobs_flat = helper_rval[2 * len(x_all): 4 * len(x_all)]
        Bobs_flat = helper_rval[4 * len(x_all):]
        Gobs = IdxsValsList.fromflattened(Gobs_flat)
        Bobs = IdxsValsList.fromflattened(Bobs_flat)
        # guard against book-keeping error
        gis = Gobs.idxset()
        bis = Bobs.idxset()
        xis = x_all.idxset()
        assert len(xis) == len(y_all)
        assert gis.union(bis) == xis
        assert gis.intersection(bis) == set()

        return IdxsValsList.fromflattened(keep_flat)


def GaussianGP(bandit):
    return GP_BanditAlgo(bandit)