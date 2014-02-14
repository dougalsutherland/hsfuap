#!/usr/bin/env python
from __future__ import division

from functools import partial

import numpy as np
import pandas as pd
from scipy import linalg

from ..misc import progress


def leverages_of_unknown(A, B, rcond=1e-15):
    # TODO: definitely a better way to do this
    # can you compute  A^{-1/2} B  without eigendecomposing A?
    #   (is solve(sqrtm(A), B) better?)

    # get pinv(sqrtm(A))
    # assume that A is actually psd; any negative eigs are noise/numerical error
    A_vals, A_vecs = np.linalg.eigh(A)
    np.maximum(A_vals, 0, out=A_vals)
    np.sqrt(A_vals, out=A_vals)
    cutoff = np.max(A_vals) * rcond
    zeros = A_vals < cutoff
    A_vals[zeros] = 0
    A_vals[~zeros] **= -1
    inv_sqrt_A = np.dot(A_vecs, A_vals.reshape(-1, 1) * A_vecs.T)

    # better way to do this:
    #   x^T A^{-1} x = || L \ x ||^2
    # where L is the Cholesky factor of A
    # can probably figure out how to use that here
    X = inv_sqrt_A.dot(B)
    S = A + X.dot(X.T)
    Y = np.linalg.pinv(S)

    return np.einsum('ki,kl,li->i', X, Y, X)


def nys_error(K, picked):
    A = K[np.ix_(picked, picked)]
    B = K[np.ix_(picked, ~picked)]
    C = K[np.ix_(~picked, ~picked)]

    A_pinv = np.linalg.pinv(A)

    Bhat = A.dot(A_pinv).dot(B)
    Berr = ((Bhat - B) ** 2).sum()

    Chat = B.T.dot(A_pinv.dot(B))
    Cerr = ((Chat - C) ** 2).sum()

    return np.sqrt(2 * Berr + Cerr)


def _run_nys(W, pick, start_n=5, max_n=None):
    # choose an initial couple of points uniformly at random
    picked = np.zeros(W.shape[0], dtype='bool')
    picked[np.random.choice(W.shape[0], start_n, replace=False)] = True
    n = picked.sum()

    if max_n is None:
        max_n = W.shape[0]

    n_picked = [n]
    n_evaled = [n]
    rmse = [nys_error(W, picked)]

    # could do this faster with woodbury, probably
    pbar = progress(maxval=max_n).start()
    pbar.update(n)
    try:
        while n_picked[-1] < max_n:
            indices, extra_evaled = pick(picked)
            picked[indices] = True
            n = picked.sum()

            n_picked.append(n)
            n_evaled.append(extra_evaled.imag if np.iscomplex(extra_evaled)
                            else n + extra_evaled)
            rmse.append(nys_error(W, picked))
            pbar.update(min(n, max_n))
    except Exception as e:
        import traceback
        traceback.print_exc()

    pbar.finish()
    return pd.DataFrame(
        {'n_picked': n_picked, 'n_evaled': n_evaled, 'rmse': rmse})

def pick_up_to(ary, n, p=None):
    if np.shape(ary) == () and ary > 0:
        ary = np.arange(ary)
    if p is not None:
        n = min(n, np.nonzero(p)[0].size)
    return np.random.choice(ary, replace=False, size=min(n, ary.shape[0]), p=p)


def run_uniform(W, start_n=5, max_n=None, step_size=1):
    return _run_nys(
        W,
        lambda picked: (pick_up_to((~picked).nonzero()[0], n=step_size), 0),
        start_n=start_n, max_n=max_n)


def run_adapt_full(W, start_n=5, max_n=None, step_size=1):
    n = W.shape[0]
    def pick(picked):
        uc, _, _ = np.linalg.svd(W[:, picked], full_matrices=False)
        err = W - uc.dot(uc.T).dot(W)
        probs = np.zeros(picked.size)
        probs[~picked] = (err[~picked, :] ** 2).sum(axis=1)
        probs /= probs.sum()
        return (pick_up_to(probs.size, p=probs, n=step_size), n*1j)
    return _run_nys(W, pick, start_n=start_n, max_n=max_n)

def run_adapt_full_lev(W, start_n=5, max_n=None, step_size=1):
    n = W.shape[0]
    def pick(picked):
        uc, _, _ = np.linalg.svd(W[:, picked], full_matrices=False)
        err = W - uc.dot(uc.T).dot(W)
        err_u, _, _ = np.linalg.svd(err)
        probs = np.zeros(picked.size)
        # using this rank for leverage scores is kind of arbitrary
        probs[~picked] = (err_u[~picked, :picked.sum()] ** 2).sum(axis=1)
        probs /= probs.sum()
        return (pick_up_to(probs.size, p=probs, n=step_size), n*1j)
    return _run_nys(W, pick, start_n=start_n, max_n=max_n)


def run_leverage_full_iter(W, start_n=5, max_n=None, step_size=1):
    # NOTE: not quite the full leverage-based algorithm.
    # That chooses leverage scores for the final rank, where
    # this does it iteratively. Not clear how different those are.
    u, s, v = np.linalg.svd(W)
    n = W.shape[0]

    def pick_by_leverage(picked):
        levs = np.zeros(n)
        levs[~picked] = (u[~picked, :picked.sum()] ** 2).sum(axis=1)
        levs /= levs.sum()
        return (pick_up_to(levs.shape[0], p=levs, n=step_size), n*1j)

    return _run_nys(W, pick_by_leverage, start_n=start_n, max_n=max_n)

def run_leverage_est(W, start_n=5, max_n=None, step_size=1):
    # Like above, but pick based on the leverage scores of \hat{W} instead
    # of W, so it doesn't use any knowledge we don't have.
    def pick_by_leverage(picked):
        levs = leverages_of_unknown(W[np.ix_(picked, picked)],
                                    W[np.ix_(picked, ~picked)])
        assert np.all(np.isfinite(levs))
        dist = levs / levs.sum()
        unpicked_idx = pick_up_to(dist.shape[0], p=dist, n=step_size)
        return ((~picked).nonzero()[0][unpicked_idx], 0)

    return _run_nys(W, pick_by_leverage, start_n=start_n, max_n=max_n)


def pick_det_greedy(picked, W, samp):
    # TODO: could build this up across runs blockwise
    chol_W_picked = linalg.cholesky(W[np.ix_(picked, picked)], lower=True)
    # det_W_picked = np.prod(np.diagonal(chol_W_picked)) ** 2
    # don't need this since it's the same for everyone

    dets = np.zeros(picked.size)
    tmps = linalg.solve_triangular(chol_W_picked, W[np.ix_(picked, ~picked)], lower=True)
    dets[~picked] = W[~picked, ~picked] - np.sum(tmps ** 2, axis=0)  # * det_W_picked

    if samp:
        return (pick_up_to(picked.size, p=dets / dets.sum(), n=1), 0)
    else:
        return (np.argmax(dets), 0)


def run_determinant_greedy_samp(W, start_n=5, max_n=None, step_size=1):
    assert step_size == 1
    f = partial(pick_det_greedy, W=W, samp=True)
    return _run_nys(W, f, start_n=start_n, max_n=max_n)

def run_determinant_greedy(W, start_n=5, max_n=None, step_size=1):
    assert step_size == 1
    f = partial(pick_det_greedy, W=W, samp=False)
    return _run_nys(W, f, start_n=start_n, max_n=max_n)


def nys_kmeans(K, x, n):
    # NOTE: doesn't make sense to do this iteratively
    from vlfeat import vl_kmeans
    from cyflann import FLANNIndex
    centers = vl_kmeans(x, num_centers=n)
    picked = FLANNIndex().nn(x, centers, num_neighbors=1)[0]
    return nys_error(K, picked)

def run_kmeans(K, X, start_n=5, max_n=None, step_size=1):
    # NOTE: not actually iterative, unlike the others
    if max_n is None:
        max_n = K.shape[0]
    ns = range(start_n, max_n + 1, step_size)
    rmses = [nys_kmeans(K, X, n) for n in progress()(ns)]
    return pd.DataFrame({'n_picked': ns, 'n_evaled': ns, 'rmse': rmses})


def _do_nys(K, picked, out):
    notpicked = ~picked
    A = K[np.ix_(picked, picked)]
    B = K[np.ix_(picked, notpicked)]

    out[np.ix_(picked, picked)] = A
    out[np.ix_(picked, notpicked)] = B
    out[np.ix_(notpicked, picked)] = B.T
    out[np.ix_(notpicked, notpicked)] = B.T.dot(np.linalg.pinv(A).dot(B))

def run_smga_frob(K, start_n=5, max_n=None, eval_size=59, step_size=1):
    assert step_size == 1

    # choose an initial couple of points uniformly at random
    N = K.shape[0]
    if max_n is None:
        max_n = N

    picked = np.zeros(N, dtype='bool')
    picked[np.random.choice(N, start_n, replace=False)] = True
    evaled = picked.copy()

    n = picked.sum()
    n_picked = [n]
    n_evaled = [n]

    est = np.empty_like(K)
    err = np.empty_like(K)
    _do_nys(K, picked, est)
    np.subtract(K, est, out=err)
    rmse = [np.linalg.norm(err, 'fro')]

    err_prods = np.empty_like(err)

    pbar = progress(maxval=max_n).start()
    pbar.update(n)

    try:
        while n_picked[-1] < max_n:
            pool = pick_up_to((~picked).nonzero()[0], n=eval_size)
            evaled[pool] = True

            np.dot(err, err, out=err_prods)  # each entry is  err[i].dot(err[j])
            imp_factors = np.array([
                (err_prods[i, :] ** 2).sum() / (err[i] ** 2).sum()
                for i in pool
            ])
            i = pool[np.argmax(imp_factors)]

            picked[i] = True
            n_picked.append(picked.sum())
            n_evaled.append(evaled.sum())

            _do_nys(K, picked, est)
            np.subtract(K, est, out=err)
            rmse.append(np.linalg.norm(err, 'fro'))

            pbar.update(n_picked[-1])
    except Exception as e:
        import traceback
        traceback.print_exc()

    pbar.finish()
    return pd.DataFrame({'n_picked': n_picked, 'n_evaled': n_evaled, 'rmse': rmse})



def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--start-n', type=int, default=5)
    parser.add_argument('--max-n', type=int, default=None)
    parser.add_argument('--step-size', type=int, default=1)
    parser.add_argument('--method', '-m', required=True)
    parser.add_argument('--kernel-file', '-k', required=True)
    parser.add_argument('--kernel-path', '-K')
    parser.add_argument('--feats-path')
    parser.add_argument('outfile')
    args = parser.parse_args()

    method = globals()['run_{}'.format(args.method)]
    if args.method == 'kmeans':
        with np.load(args.feats_path) as d:
            X = np.sqrt(d['hists'])
        method = partial(method, X=X)

    if args.kernel_path:
        import h5py
        with h5py.File(args.kernel_file, 'r') as f:
            kernel = f[args.kernel_path][()]
    else:
        kernel = np.load(args.kernel_file)

    n, m = kernel.shape
    assert n == m

    d = method(kernel, start_n=args.start_n, max_n=args.max_n,
               step_size=args.step_size)
    d.to_csv(args.outfile)

if __name__ == '__main__':
    main()
