#  OpenTS: Open exoplanet transit search pipeline.
#  Copyright (C) 2015-2020  Hannu Parviainen
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.
from collections import namedtuple
from logging import getLogger
from pathlib import Path
from typing import Optional

from astropy.io.fits import HDUList, Card
from numba import njit
import pandas as pd
from astropy.table import Table
from matplotlib.pyplot import setp
from numpy import ones, unique, argsort, atleast_2d, ndarray, squeeze, inf, isfinite, exp, concatenate
from numpy.core._multiarray_umath import floor, zeros, log, pi, array, sin
from pytransit import QuadraticModelCL, QuadraticModel, BaseLPF
from pytransit.lpf.tesslpf import downsample_time
from pytransit.orbits import epoch
from pytransit.param import LParameter, UniformPrior as UP, PParameter
from pytransit.utils.misc import fold
from scipy.interpolate import interp1d

from .otsstep import OTSStep
from .plots import bplot

logger = getLogger("transit-fit-step")


@njit(fastmath=True)
def sine_model(time, period, phase, amplitudes):
    npv = period.size
    npt = time.size
    nsn = amplitudes.shape[1]

    bl = zeros((npv, npt))
    for i in range(npv):
        for j in range(nsn):
            bl[i, :] += amplitudes[i, j] * sin(2 * pi * (time - phase[i] * period[i]) / (period[i] / (j + 1)))
    return bl


def delta_bic(dll, d1, d2, n):
    return dll + 0.5 * (d1 - d2) * log(n)

class SineBaseline:
    def __init__(self, lpf, name: str = 'sinbl', n: int = 1, lcids=None):
        self.name = name
        self.lpf = lpf
        self.n = n

        if lpf.lcids is None:
            raise ValueError('The LPF data needs to be initialised before initialising LinearModelBaseline.')

        self.init_data(lcids)
        self.init_parameters()

    def init_data(self, lcids=None):
        self.time = self.lpf.timea - self.lpf._tref

    def init_parameters(self):
        """Baseline parameter initialisation.
        """
        fptp = self.lpf.ofluxa.ptp()
        bls = []
        bls.append(LParameter(f'c_sin', f'sin phase', '', UP(0.0, 1.0), bounds=(0, 1)))
        for i in range(self.n):
            bls.append(LParameter(f'a_sin_{i}', f'sin {i} amplitude', '', UP(0, fptp), bounds=(0, inf)))
        self.lpf.ps.thaw()
        self.lpf.ps.add_global_block(self.name, bls)
        self.lpf.ps.freeze()
        self.pv_slice = self.lpf.ps.blocks[-1].slice
        self.pv_start = self.lpf.ps.blocks[-1].start
        setattr(self.lpf, f"_sl_{self.name}", self.pv_slice)
        setattr(self.lpf, f"_start_{self.name}", self.pv_start)

    def __call__(self, pvp, bl: Optional[ndarray] = None):
        pvp = atleast_2d(pvp)
        if bl is None:
            bl = ones((pvp.shape[0], self.time.size))
        else:
            bl = atleast_2d(bl)

        bl += sine_model(self.time,
                         period=pvp[:, 1],
                         phase=pvp[:, self.pv_start],
                         amplitudes=pvp[:, self.pv_start + 1:])
        return squeeze(bl)


class SearchLPF(BaseLPF):
    # def _init_lnlikelihood(self):
    #    self._add_lnlikelihood_model(CeleriteLogLikelihood(self))

    def _init_baseline(self):
        self._add_baseline_model(SineBaseline(self, n=1))

    def _init_p_limb_darkening(self):
        pld = concatenate([
            [PParameter(f'q1', 'q1 coefficient', '', UP(0, 1), bounds=(0, 1)),
             PParameter(f'q2', 'q2 coefficient', '', UP(0, 1), bounds=(0, 1))]
            for i, pb in enumerate(self.passbands)])
        self.ps.add_passband_block('ldc', 2, self.npb, pld)
        self._sl_ld = self.ps.blocks[-1].slice
        self._start_ld = self.ps.blocks[-1].start

class TransitFitStep(OTSStep):

    def __init__(self, ts, mode: str, title: str, nsamples: int = 1, exptime: float = 1, use_opencl: bool = False, use_tqdm: bool = True):
        assert mode in ('all', 'even', 'odd')
        super().__init__(ts)
        self.mode = mode
        self.title = title
        self.nsamples = nsamples
        self.exptime = exptime
        self.use_opencl = use_opencl
        self.use_tqdm = use_tqdm
        self.lpf = None
        self.result = None

        self.mask = None
        self.time = None
        self.phase = None
        self.fobs = None
        self.fmod = None
        self.ftra = None

        self.dll_epochs = None
        self.dll_values = None
        self.parameters = None

        self.period = None      # Best-fit period
        self.zero_epoch = None  # Best-fit zero epoch
        self.duration = None    # Best-fit duration
        self.depth = None       # Best-fit depth

    def __call__(self, npop: int = 30, de_niter: int = 1000, mcmc_niter: int = 100, mcmc_repeats: int = 2, initialize_only: bool = False):
        logger.info(f"Fitting {self.mode} transits")
        self.ts.transit_fits[self.mode] = self

        epochs = epoch(self.ts.time, self.ts.zero_epoch, self.ts.period)

        if self.mode == 'all':
            mask = ones(self.ts.time.size, bool)
        elif self.mode == 'even':
            mask = epochs % 2 == 0
        elif self.mode == 'odd':
            mask = epochs % 2 == 1
        else:
            raise NotImplementedError

        mask &= abs(self.ts.phase - 0.5*self.ts.period) < 4 * 0.5 * self.ts.duration

        self.ts.transit_fit_masks[self.mode] = self.mask = mask

        epochs = epochs[mask]
        self.time = self.ts.time[mask]
        self.fobs = self.ts.flux[mask]

        tref = floor(self.time.min())
        tm = QuadraticModelCL(klims=(0.01, 0.60)) if self.use_opencl else QuadraticModel(interpolate=False)
        self.lpf = lpf = SearchLPF('transit_fit', [''], times=self.time, fluxes=self.fobs, tm=tm,
                        nsamples=self.nsamples, exptimes=self.exptime, tref=tref)

        if self.mode == 'all':
            lpf.set_prior('tc', 'NP', self.ts.zero_epoch, 0.01)
            lpf.set_prior('p', 'NP', self.ts.period, 0.001)
            lpf.set_prior('k2', 'UP', 0.5 * self.ts.depth, 2 * self.ts.depth)
        else:
            pr = self.ts.tf_all.parameters
            lpf.set_prior('tc', 'NP', pr.tc.med, pr.tc.err)
            lpf.set_prior('p', 'NP', pr.p.med, pr.p.err)
            lpf.set_prior('k2', 'UP', max(0.01**2, 0.5 * pr.k2.med), max(0.08**2, min(0.6**2, 2 * pr.k2.med)))
            lpf.set_prior('q1', 'NP', pr.q1.med, pr.q1.err)
            lpf.set_prior('q2', 'NP', pr.q2.med, pr.q2.err)

        # TODO: The limb darkening table has been computed for TESS. Needs to be made flexible.
        if self.ts.teff is not None:
            ldcs = Table.read(Path(__file__).parent / "data/ldc_table.fits").to_pandas()
            ip = interp1d(ldcs.teff, ldcs[['q1', 'q2']].T)
            q1, q2 = ip(self.ts.teff)
            lpf.set_prior('q1', 'NP', q1, 1e-5)
            lpf.set_prior('q2', 'NP', q2, 1e-5)

        if initialize_only:
            return
        else:
            lpf.optimize_global(niter=de_niter, npop=npop, use_tqdm=self.use_tqdm, plot_convergence=False)
            lpf.sample_mcmc(mcmc_niter, repeats=mcmc_repeats, use_tqdm=self.use_tqdm)
            df = lpf.posterior_samples(derived_parameters=True)
            df = pd.DataFrame((df.median(), df.std()), index='med err'.split())
            pv = lpf.posterior_samples(derived_parameters=False).median().values
            self.phase = fold(self.time, pv[1], pv[0], 0.5) * pv[1] - 0.5 * pv[1]
            self.fmod = lpf.flux_model(pv)
            self.ftra = lpf.transit_model(pv)

            # Calculate the per-orbit log likelihood differences
            # --------------------------------------------------
            ues = unique(epochs)
            lnl = zeros(ues.size)
            err = 10 ** pv[7]

            def lnlike_normal(o, m, e):
                npt = o.size
                return -npt * log(e) - 0.5 * npt * log(2. * pi) - 0.5 * sum((o - m) ** 2 / e ** 2)

            for i, e in enumerate(ues):
                m = epochs == e
                lnl[i] = lnlike_normal(self.fobs[m], self.fmod[m], err) - lnlike_normal(self.fobs[m], 1.0, err)

            self.parameters = df
            self.dll_epochs = ues
            self.dll_values = lnl

            self.zero_epoch = df.tc.med
            self.period = df.p.med
            self.duration = df.t14.med
            self.depth = df.k2.med

            if self.mode == 'all':
                self.delta_bic = self.ts.dbic = delta_bic(lnl.sum(), 0, 9, self.time.size)
            self.ts.update_ephemeris(self.zero_epoch, self.period, self.duration, self.depth)


    def add_to_fits(self, hdul: HDUList):
        if self.lpf is not None:
            p = self.parameters
            c = self.mode[0]
            h = hdul[0].header
            h.append(Card('COMMENT', '======================'))
            h.append(Card('COMMENT', self.title))
            h.append(Card('COMMENT', '======================'))
            h.append(Card(f'TF{c}_T0', p.tc.med, 'Transit centre [BJD]'), bottom=True)
            h.append(Card(f'TF{c}_T0E', p.tc.err, 'Transit centre uncertainty [d]'), bottom=True)
            h.append(Card(f'TF{c}_PR', p.p.med, 'Orbital period [d]'), bottom=True)
            h.append(Card(f'TF{c}_PRE', p.p.err, 'Orbital period uncertainty [d]'), bottom=True)
            h.append(Card(f'TF{c}_RHO', p.rho.med, 'Stellar density [g/cm^3]'), bottom=True)
            h.append(Card(f'TF{c}_RHOE', p.rho.err, 'Stellar density uncertainty [g/cm^3]'), bottom=True)
            h.append(Card(f'TF{c}_B', p.b.med, 'Impact parameter'), bottom=True)
            h.append(Card(f'TF{c}_BE', p.b.err, 'Impact parameter uncertainty'), bottom=True)
            h.append(Card(f'TF{c}_AR', p.k2.med, 'Area ratio'), bottom=True)
            h.append(Card(f'TF{c}_ARE', p.k2.err, 'Area ratio uncertainty'), bottom=True)
            h.append(Card(f'TF{c}_SC', p.c_sin.med, 'Sine phase'), bottom=True)
            h.append(Card(f'TF{c}_SCE', p.c_sin.err, 'Sine phase uncertainty'), bottom=True)
            h.append(Card(f'TF{c}_SA', p.a_sin_0.med, 'Sine amplitude'), bottom=True)
            h.append(Card(f'TF{c}_SAE', p.a_sin_0.err, 'Sine amplitude uncertainty'), bottom=True)
            h.append(Card(f'TF{c}_RR', p.k.med, 'Radius ratio'), bottom=True)
            h.append(Card(f'TF{c}_RRE', p.k.err, 'Radius ratio uncertainty'), bottom=True)
            h.append(Card(f'TF{c}_A', p.a.med, 'Semi-major axis'), bottom=True)
            h.append(Card(f'TF{c}_AE', p.a.err, 'Semi-major axis uncertainty'), bottom=True)
            h.append(Card(f'TF{c}_T14', p.t14.med, 'Transit duration T14 [d]'), bottom=True)
            h.append(Card(f'TF{c}_T14E', p.t14.err, 'Transit duration T14 uncertainty [d]'), bottom=True)
            if isfinite(p.t23.med) and isfinite(p.t23.err):
                h.append(Card(f'TF{c}_T23', p.t23.med, 'Transit duration T23 [d]'), bottom=True)
                h.append(Card(f'TF{c}_T23E', p.t23.err, 'Transit duration T23 uncertainty [d]'), bottom=True)
                h.append(Card(f'TF{c}_TDR', p.t23.med / p.t14.med, 'T23 to T14 ratio'), bottom=True)
            else:
                h.append(Card(f'TF{c}_T23', 0, 'Transit duration T23 [d]'), bottom=True)
                h.append(Card(f'TF{c}_T23E', 0, 'Transit duration T23 uncertainty [d]'), bottom=True)
                h.append(Card(f'TF{c}_TDR', 0, 'T23 to T14 ratio'), bottom=True)
            h.append(Card(f'TF{c}_WN', 10 ** p.wn_loge_0.med, 'White noise std'), bottom=True)
            h.append(Card(f'TF{c}_GRAZ', p.b.med + p.k.med > 1., 'Is the transit grazing'), bottom=True)

            ep = self.dll_epochs
            ll = self.dll_values

            lm = ll.max()
            h.append(Card(f'TF{c}_DLLA', log(exp(ll - lm).mean()) + lm, 'Mean per-orbit delta log likelihood'), bottom=True)
            if self.mode == 'all':
                m = ep % 2 == 0
                lm = ll[m].max()
                h.append(Card(f'TFA_DLLO', log(exp(ll[m] - lm).mean()) + lm, 'Mean per-orbit delta log likelihood (odd)'),
                         bottom=True)
                m = ep % 2 != 0
                lm = ll[m].max()
                h.append(Card(f'TFA_DLLE', log(exp(ll[m] - lm).mean()) + lm, 'Mean per-orbit delta log likelihood (even)'),
                         bottom=True)

    @bplot
    def plot_transit_fit(self, ax=None, full_phase: bool = False, mode='all', nbins: int = 20, alpha=0.2):
        zero_epoch, period, duration = self.parameters[['tc', 'p', 't14']].iloc[0].copy()
        hdur = duration * array([-0.5, 0.5])

        phase = self.phase
        sids = argsort(phase)
        phase = phase[sids]
        pmask = ones(phase.size, bool) if full_phase else abs(phase) < 1.5 * duration

        if pmask.sum() < 100:
            alpha = 1

        fmod = self.fmod[sids]
        fobs = self.fobs[sids]
        ax.plot(24 * phase[pmask], fobs[pmask], '.', alpha=alpha)
        ax.plot(24 * phase[pmask], fmod[pmask], 'k')

        if duration > 1 / 24:
            pb, fb, eb = downsample_time(phase[pmask], fobs[pmask], phase[pmask].ptp() / nbins)
            ax.errorbar(24 * pb, fb, eb, fmt='ok')
            ylim = fb.min() - 2 * eb.max(), fb.max() + 2 * eb.max()
        else:
            ylim = fobs[pmask].min(), fobs[pmask].max()

        ax.text(24 * 2.5 * hdur[0], fmod.min(), f'$\Delta$F {1 - fmod.min():6.4f}', size=10, va='center',
                bbox=dict(color='white'))
        ax.axhline(fmod.min(), alpha=0.25, ls='--')

        ax.get_yaxis().get_major_formatter().set_useOffset(False)
        ax.axvline(0, alpha=0.25, ls='--', lw=1)
        [ax.axvline(24 * hd, alpha=0.25, ls='-', lw=1) for hd in hdur]

        ax.autoscale(axis='x', tight='true')
        setp(ax, ylim=ylim, xlabel='Phase [h]', ylabel='Normalised flux')

    @bplot
    def plot_folded_and_binned_lc(self, ax=None, nbins: int = 100):
        phase = self.phase
        sids = argsort(phase)
        phase = phase[sids]
        flux_o = self.fobs[sids]
        flux_m = self.fmod[sids]

        pb, fb, eb = downsample_time(phase, flux_o, phase.ptp() / nbins)
        _, fob, _ = downsample_time(phase, flux_m, phase.ptp() / nbins)

        ax.errorbar(pb, fb, eb)
        ax.plot(pb, fob, 'k')
        ax.autoscale(axis='x', tight=True)
        setp(ax, xlabel='Phase [d]', ylabel='Normalized flux')
