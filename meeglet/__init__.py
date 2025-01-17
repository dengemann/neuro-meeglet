__version__ = "0.0.1"
# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/api/wavelets.ipynb.

# %% auto 0
__all__ = ['define_frequencies', 'define_wavelets', 'compute_spectral_features_array', 'compute_spectral_features',
           'spectrum_from_features', 'ro_corrcoef', 'bw2qt', 'qt2bw', 'plot_wavelet_family']

# %% ../nbs/api/wavelets.ipynb 2
import warnings
from types import SimpleNamespace
from typing import Union, Optional
from math import nan, sqrt, log, log2, pi, ceil
import cmath
import numpy as np

from mne.utils import logger, verbose
from mne.io.base import BaseRaw
from mne import BaseEpochs

from pathlib import Path
import numpy as np
from numpy.testing import assert_array_equal, assert_array_almost_equal
from scipy.io import loadmat
from scipy import stats
import mne
from mne.datasets.testing import requires_testing_data

import pyriemann

import pytest

import matplotlib.pyplot as plt
import matplotlib as mpl

# %% ../nbs/api/wavelets.ipynb 4
def define_frequencies(
        foi_start: float=2, # The lowest frequency of interest.
        foi_end: float=32, # The highest frequency of interest. 
        delta_oct: Union[float, None]=None, #  Controls the frequency resolution. If None, defaults
                                    # to bw_oct / 4. If 1, spacing between frequencies of interesrt will be 1 octave,
                                    # e.g. for foi_start=2 and foi_end=32 foi will be (2, 4, 8, 16, 32).
        bw_oct: float=0.5, # The bandwidth of the Wavelets in octaves. Larger band width lead to more smoothing.
        qt: Union[float, None]=None, # The bandwidth of the Wavelets expressed in characteristic Morlet parameter Q (overriding bw_oct).
        freq_shift_factor: int=1, # Allows shifting the frequency spectrum in logarithmic space (in octave units).
    ) -> (np.ndarray, np.ndarray, np.ndarray, np.ndarray): # `foi`, the expaneded frequency range, `sigma_time`, the temporal width (SD), `sigma_freq`, the spectral width. 
    "Construct log-space equidistant frequency bins with proportional variance."
    if bw_oct is not None and qt is not None:
        raise ValueError('Choose bw_oct or qt, not both.')
    elif qt is not None:
        bw_oct = qt2bw(qt)
    elif bw_oct is not None:
        qt = bw2qt(bw_oct)
    else:
        raise ValueError('Please pass bw_oct or qt, at least one of them.')

    assert bw_oct is not None
    
    if delta_oct is None:
        delta_oct = bw_oct / 4.

    foi = 2 ** np.arange(log2(foi_start), log2(foi_end + 1 / 1e5), delta_oct)
    foi *= freq_shift_factor
    foi_min = 2 * foi / (2 ** bw_oct + 1)  # arithmetic mean
    foi_max = 2 * foi / (2 ** -bw_oct + 1)
    # std in freq domain, then in time domain
    sigma_freq = (foi_max - foi_min) / (2 * sqrt(2 * log(2)))
    sigma_time = 1. / (2 * pi * sigma_freq)
    return foi, sigma_time, sigma_freq, bw_oct, qt


def define_wavelets(
        foi: np.ndarray,  # The range of center frequencies.
        sigma_time: np.ndarray, # The temporal width (standard deviations) at a given frequency.
        sfreq: float, # The sampling frequency in Hz.
        kernel_width: int=5, # The width of the kernel in standard deviations, leading to truncation.
        window_shift: float=0.25, # Controls the spacing of the sliding windows proportionally to the
                                  # length (seconds) of the wavelet kernel. Depends on the frequency of
                                   #interest. Values smaller than 1 lead to overlapping sliding windows.
        density: str='oct', # Scaling of the power spectrum in Hz or per octave ('oct'). Defaults to 'oct'.
                            # Note that this scaling is defined at the level of Wavelet kernels, hence,
                            # applies to all derived quantities.
    ) -> list: # The list of complex Morlet wavelets alongside the scaling applied, the effective number of samples and the amount of samples shifted in time, ordered by input frequencies.
    "Compute Morelt Wavelets from frequency-domain parametrization."
    wavelets = list()
    scaling = sqrt(2.0 / sfreq)
    for i_foi in range(len(foi)):
        n_samp_eff = np.int64(np.ceil(kernel_width * sigma_time[i_foi] * sfreq + 1))
        n_shift = np.int64(np.ceil(np.float64(n_samp_eff) * window_shift))
        tt = (np.arange(1, n_samp_eff + 1, 1) - n_samp_eff / 2 - 0.5) / sfreq
        zz = tt / sigma_time[i_foi]
        taper = np.exp(-(1 / 2) * zz ** 2)
        taper /= np.sqrt(np.sum(np.abs(taper) ** 2))
        i_exp = np.exp(1j * 2 * pi * foi[i_foi] * tt)
        kernel = (taper * i_exp)[:, None]
        if density == 'Hz':
            scaling = sqrt(2.0 / sfreq)
        elif density == 'oct':
            scaling = sqrt(2.0 / sfreq) * sqrt(log(2) * foi[i_foi])

        wavelets.append((kernel, scaling, n_samp_eff, n_shift))
    return wavelets

# %% ../nbs/api/wavelets.ipynb 6
def _apply_wavlet(data, kernel, scaling, n_samp_eff, n_shift, allow_fraction_nan):
    "Apply Morlet Wavelets to data and handle NaNs."
    n_sens, n_sample = data.shape
    nan_time_idx = np.diff(np.isnan(np.sum(data, axis=0)).astype(int), 
                                    prepend=0)
    idx_up = np.where(nan_time_idx == 1)[0]
    idx_down = np.where(nan_time_idx == -1)[0]
    nan_width_init = np.zeros((data.shape[1]))

    iter_range = list(range(0, data.shape[1] - n_samp_eff + 1, n_shift))
    data_conv = np.empty((n_sens, len(iter_range)), dtype=np.complex128)
    data_conv[:] = np.nan

    # memory allocation
    frac_nan = np.empty((data_conv.shape[1]))
    frac_nan[:] = np.nan

    # handle nans
    nan_width = nan_width_init.copy()
    for i_nan in range(len(idx_up) - 1):
        up, down = idx_up[i_nan], idx_down[i_nan]
        nan_width[up:down] = down - up
    if len(idx_up) > len(idx_down):
        nan_width[idx_up[-1]:-1] = len(nan_width) + 1 - idx_up[-1]

    # convolution
    for cnt, i_section in enumerate(iter_range):
        section = np.float64(data[:, i_section:i_section + n_samp_eff])
        nan_width_section = nan_width[i_section:i_section + n_samp_eff]
        n_nan = np.sum(np.isnan(section[0]))
        frac_nan[cnt] = n_nan / section.shape[1]
        allow_nan_limit = section.shape[1] * allow_fraction_nan
        if n_nan == 0:
            data_conv[:, cnt:cnt + 1] = (
                section @ np.flip(kernel, axis=0) * scaling
            )
        elif ((n_nan < allow_nan_limit) &
                (np.max(nan_width_section) < allow_nan_limit)):
            idx_valid = np.where(~np.isnan(section[0, :]))[0]
            kernel_tmp = np.flip(kernel, axis=0)
            kernel_tmp = (
                kernel_tmp[idx_valid] / 
                np.sqrt(np.sum(np.abs(kernel_tmp[idx_valid]) ** 2))
            )
            # mirror image for convolution operation
            data_conv[:, cnt:cnt + 1] = (
                section[:, idx_valid] @ kernel_tmp * scaling
            )
        else:
            nan_section = np.empty(section.shape[0])
            nan_section[:] = np.nan 
            data_conv[:, cnt] = nan_section

    # derive metrics for frequency-transformed data
    idx_valid = np.where(~np.isnan(data_conv[0, :]))[0]
    n_valid = len(idx_valid)
    data_conv = data_conv[:, idx_valid]
    frac_nan = frac_nan[idx_valid]
    out = None
    if n_valid > 0:
        out = data_conv, n_valid, frac_nan

    return out


def _prepare_output(data, foi, features):
    "Initialize output datastructures."
    n_sens, _ = data.shape
    out = SimpleNamespace()
    info = SimpleNamespace()
    info.n_valid_total = np.empty(len(foi), dtype=np.int64)
    info.foi = foi
    if 'pow' in features:
        out.pow = np.empty((n_sens, len(foi)), dtype = np.float64)
        out.pow_geo = out.pow.copy()
        out.pow_median = out.pow.copy()
        out.pow_var = out.pow.copy()
    if any(k in features for k in ('csd', 'cov', 'cov_oas', 'coh', 'icoh', 'gim')):
        out.csd = np.zeros((n_sens, n_sens, len(foi)), dtype=np.complex128)
    if 'cov' in features or 'cov_oas' in features:
        out.cov = np.zeros((n_sens, n_sens, len(foi)), dtype=np.float64)
    if 'cov_oas' in features:
        out.cov_oas = np.zeros((n_sens, n_sens, len(foi)), dtype=np.float64)
    if 'coh' in features or 'icoh' in features:
        out.coh = np.empty((n_sens, n_sens, len(foi)), dtype=np.complex128)
    if 'icoh' in features:
        out.icoh = np.empty((n_sens, n_sens, len(foi)), dtype=np.float64)
    if 'gim' in features:
        out.gim = np.zeros(len(foi), dtype=np.float64)
    if 'plv' in features:
        out.plv = np.empty((n_sens, n_sens, len(foi)), dtype=np.complex128)
    if 'pli' in features:
        out.pli = np.empty((n_sens, n_sens, len(foi)), dtype=np.float64)
    if 'dwpli' in features:
        out.dwpli = np.zeros((n_sens, n_sens, len(foi)), dtype=np.float64)
    if 'r_plain' in features:
        out.r_plain = np.zeros((n_sens, n_sens, len(foi)), dtype=np.float64)
    if 'r_orth' in features:
        out.r_orth = np.zeros((n_sens, n_sens, len(foi)), dtype=np.float64)
    not_implemented = ()
    for features in features:
        if features in not_implemented:
            raise NotImplementedError(f'{features} is not implemented.')

    return out, info


@verbose
def _compute_spectral_features(data, wavelets, features, out, info,
                               allow_fraction_nan, rank, verbose):
    "Apply wavelet and compute spectral features."
    logger.info(f'Computing convolutions for {len(wavelets)}'
                f' wavelet{"s" if len(wavelets) > 1 else ""}'
                f' and extracting features ...')
    for i_foi, (kernel, scaling, n_samp_eff, n_shift) in enumerate(wavelets):
        data_conv, n_valid, frac_nan = None, None, None
        conv_ = _apply_wavlet(
            data=data, kernel=kernel, n_samp_eff=n_samp_eff,
            n_shift=n_shift, scaling=scaling,
            allow_fraction_nan=allow_fraction_nan)
        if conv_ is not None:
            data_conv, n_valid, frac_nan = conv_
        else:
            logger.warning(f"Found no valid data at {info.foi[i_foi]} Hz.")
            continue

        # power measures
        info.n_valid_total[i_foi] = n_valid
        if 'pow' in features:
            pow = np.abs(data_conv) ** 2
            out.pow[:, i_foi] = np.mean(pow, axis=1)
            out.pow_median[:, i_foi] = np.median(pow, axis=1)
            out.pow_geo[:, i_foi] = np.exp(np.mean(np.log(pow), axis=1))
            out.pow_var[:, i_foi] = np.var(pow, axis=1, ddof=1)

        if any(k in features for k in ('csd', 'cov', 'cov_oas', 'coh', 'icoh', 'gim')):
            out.csd[:, :, i_foi] = data_conv @ data_conv.conj().T  / n_valid

        if 'cov' in features or 'cov_oas' in features:
            out.cov[:, :, i_foi] = np.real(out.csd[:, :, i_foi])

        if 'cov_oas' in features:
            out.cov_oas[:, :, i_foi] = out.cov[:, :, i_foi]
            # The following code is adapted from scikit-learn implementation of
            # Oracle Approximating Shrinkage (OAS) for covariance regularization.
            emp_cov = out.cov_oas[:, :, i_foi]
            n_features = emp_cov.shape[0]
            mu = np.trace(emp_cov) / n_features
            # formula from Chen et al.'s **implementation**
            alpha = np.mean(emp_cov ** 2)
            num = alpha + mu ** 2

            n_samples = n_valid  # use effective number of samples 

            den = (n_samples + 1.0) * (alpha - (mu**2) / n_features)

            shrinkage = 1.0 if den == 0 else min(num / den, 1.0)
            shrunk_cov = (1.0 - shrinkage) * emp_cov
            shrunk_cov.flat[:: n_features + 1] += shrinkage * mu
            out.cov_oas[:, :, i_foi] = shrunk_cov

        # coherence measures
        if 'coh' in features or 'icoh' in features:
            csd = out.csd
            out.coh[:, :, i_foi] = (
                csd[:, :, i_foi] /
                np.sqrt(np.diag(csd[:, :, i_foi])[:, None] @ 
                        np.diag(csd[:, :, i_foi])[None,:])
            )

        if 'icoh' in features:
            out.icoh[:, :, i_foi] = out.coh[:, :, i_foi].imag

        if 'gim' in features:
            C = out.csd[:, :, i_foi]
            if rank < C.shape[0]:
                C_inv = ro_pinv(C.real, rank)
            else:
                C_inv = np.linalg.pinv(C.real)
            out.gim[i_foi] = 1 / 2 * np.trace(
                C_inv @ np.imag(C) @ C_inv @ np.imag(C).T
            )

        # phase measures
        if 'plv' in features:
            data_n = data_conv / np.abs(data_conv)
            out.plv[:, :, i_foi] = data_n @ data_n.conj().T / n_valid

        if 'pli' in features:
            n_sens = data.shape[0]
            data_n = data_conv / np.abs(data_conv)
            for i_idx in range(n_sens):
                for j_idx in range(i_idx + 1, n_sens, 1):
                    out.pli[i_idx, j_idx, i_foi] = np.mean(
                        np.sign(np.imag(data_n[i_idx] * data_n[j_idx].conj()))
                    )
            out.pli[:, :, i_foi] = out.pli[:, :, i_foi] + out.pli[:, :, i_foi].T

        if 'dwpli' in features:
            n_sens = data.shape[0]
            for i_idx in range(n_sens):
                for j_idx in range(i_idx + 1, n_sens, 1):
                    cdi = np.imag(data_conv[i_idx] * np.conj(data_conv[j_idx]))
                    imag_sum = np.sum(cdi)
                    imag_sum_w = np.sum(np.abs(cdi))
                    debias_factor = np.sum(cdi ** 2)
                    out.dwpli[i_idx, j_idx, i_foi]  = (
                        (imag_sum ** 2 - debias_factor) /
                        (imag_sum_w ** 2 - debias_factor)
                    )
            out.dwpli[:, :, i_foi] = out.dwpli[:, :, i_foi] + out.dwpli[:, :, i_foi].T

        # envelope correlation measures
        if any(ft in features for ft in ('r_plain', 'r_orth')):
            for i_sens in range(data.shape[0]):
                seed = data_conv[i_sens]
                seed_logpow = np.log(seed * seed.conj())
                src = data_conv
                src_logpow = np.log(src * src.conj())
                if any('orth' in ft for ft in features):
                    seed_abs = (seed / np.abs(seed))[np.newaxis]
                    src_orth = np.imag(data_conv * np.conj(seed_abs)) * cmath.sqrt(-1) * seed_abs
                    src_logpow_orth = np.log(src_orth * np.conj(src_orth))
                if 'r_plain' in features:
                    r_plain = ro_corrcoef(seed_logpow[np.newaxis], src_logpow, 2)
                    out.r_plain[i_sens, :, i_foi] = r_plain.r.real
                if 'r_orth' in features:
                    r_orth = ro_corrcoef(seed_logpow[np.newaxis], src_logpow_orth, 2)
                    out.r_orth[i_sens, :, i_foi] = r_orth.r.real
                    # make sure we have nans on diag as in Matlab
        else:
            # implement other options here in the future
            pass
    logger.info('done')


def _prepand_nan_epochs(data):
    "Prepends 1 sample of NaN values to every eoch."
    if not data.ndim == 3:
        raise ValueError(f'Data must be 3-dimensional, got {data.ndim} dimensions.')
    nans = [np.nan for _ in range(data.shape[1])]
    data = [np.c_[nans, ep] for ep in data]
    return np.array(data)


def _set_nan_from_annotations_raw(raw, data, annotations):
    "Set nan values to data where bad annotations are present"
    for annot in annotations:
        if annot['description'].lower().startswith('bad'):
            start = annot['onset']
            stop = start + annot['duration']
            start_idx = raw.time_as_index(start, use_rounding=True,
                                          origin=annot['orig_time'])[0]
            stop_idx = raw.time_as_index(stop, use_rounding=True,
                                         origin=annot['orig_time'])[0]
            data[:, start_idx:stop_idx] = np.nan

# %% ../nbs/api/wavelets.ipynb 7
@verbose
def compute_spectral_features_array(
        data: np.ndarray, # The continously sampled input data (may contain NaNs),
                          # shape (n_channels, n_samples))
        sfreq: float, # The sampling frequency in Hz.
        delta_oct: Union[float, None]=None, #  Controls the frequency resolution. If None, defaults
                                    # to bw_oct / 4. If 1, spacing between frequencies of interesrt will be 1 octave,
                                    # e.g. for foi_start=2 and foi_end=32 foi will be (2, 4, 8, 16, 32).
        bw_oct: float=0.5, # The bandwidth of the Wavelets in octaves. Larger band width lead to more smoothing.
        qt: Union[float, None]=None, # The bandwidth of the Wavelets expressed in characteristic Morlet parameter Q (overriding bw_oct).
        foi_start: float=2, # The lowest frequency of interest.
        foi_end: float=32, # The highest frequency of interest. 
        window_shift: float=0.25, # Controls the spacing of the sliding windows proportionally to the
                                  # length (seconds) of the wavelet kernel. Depends on the frequency of
                                   #interest. Values smaller than 1 lead to overlapping sliding windows.
        kernel_width: int=5, # The width of the kernel in standard deviations, leading to truncation.
        freq_shift_factor: int=1, # Allows shifting the frequency spectrum in logarithmic space (in octave units).
        allow_fraction_nan: int=0, # The fraction of NA values allowed.
        features: Union[tuple, list]=('pow',), # The spectral featueres to be computed. 
        density: str='oct', # Scaling of the power spectrum in Hz or per octave ('oct'). Defaults to 'oct'.
                            # Note that this scaling is defined at the level of Wavelet kernels, hence,
                            # applies to all derived quantities.
        rank: Union[int, None]=None, # numeric rank of the input
        verbose: Union[bool, int, str]=False # `mne.verbose` for details. Should only be passed as a keyword argument.
    ) -> (SimpleNamespace, SimpleNamespace): # The `features` with, e.g., `.pow`, `.cov` as attributes
                                             # and `info` outputs with `.foi` and `.n_valid_total` attributes.
    # Compute spectral features from complex Morlet Wavelet transform.

    logger.info('Initializing Wavelets ...')
    foi, sigma_time, *_, bw_oct, qt, = define_frequencies(
        foi_start=foi_start, foi_end=foi_end, delta_oct=delta_oct,
        bw_oct=bw_oct, qt=qt, freq_shift_factor=freq_shift_factor)

    wavelets = define_wavelets(
        foi=foi, sigma_time=sigma_time, kernel_width=kernel_width,
        sfreq=sfreq, window_shift=window_shift, density=density)
    logger.info('done')

    if freq_shift_factor != 1:
        foi /= freq_shift_factor

    out, info = _prepare_output(data, foi=foi, features=features)
    info.bw_oct = bw_oct
    info.qt = qt

    if rank is None:
        rank_ = data.shape[0]
    else:
        rank_ = rank

    _compute_spectral_features(data=data, wavelets=wavelets,
                               features=features, out=out, info=info,
                               allow_fraction_nan=allow_fraction_nan,
                               rank=rank_,
                               verbose=verbose)
    return out, info


@verbose
def compute_spectral_features(
        inst: Union[mne.io.Raw, mne.Epochs], #  An MNE object representing raw (continous) or epoched data.
        delta_oct: Union[float, None]=None, #  Controls the frequency resolution. If None, defaults
                                    # to bw_oct / 4. If 1, spacing between frequencies of interesrt 
                                    # will be 1 octave, e.g. for foi_start=2 and foi_end=32 foi will
                                    # be (2, 4, 8, 16, 32).
        bw_oct: float=0.5, # The bandwidth of the Wavelets in octaves. Larger band width lead to more smoothing.
        qt: Union[float, None]=None, # The bandwidth of the Wavelets expressed in characteristic Morlet parameter Q (overriding bw_oct).
        foi_start: float=2, # The lowest frequency of interest.
        foi_end: float=32, # The highest frequency of interest. 
        window_shift: float=0.25, # Controls the spacing of the sliding windows proportionally to the
                                  # length (seconds) of the wavelet kernel. Depends on the frequency of
                                   #interest. Values smaller than 1 lead to overlapping sliding windows.
        kernel_width: int=5, # The width of the kernel in standard deviations, leading to truncation.
        freq_shift_factor: int=1, # Allows shifting the frequency spectrum in logarithmic space (in octave units).
        allow_fraction_nan: int=0, # The fraction of NA values allowed.
        features: Union[tuple, list]=('pow',), # The spectral featueres to be computed. 
        density: str='oct', # Scaling of the power spectrum in Hz or per octave ('oct'). Defaults to 'oct'.
                            # Note that this scaling is defined at the level of Wavelet kernels, hence,
                            # applies to all derived quantities.
                            # levels of smoothing across frequencies. Requires density set to 'Hz'.
        nan_from_annotations: bool=False, # If annotations should be converted to missing values. Currently only
                                          # supported for raw data. When using epochs, please take care of selecting
                                          # epochs yourself.
        prepend_nan_epochs: bool=False, #  Whether to add a Nan value at the beginning of each epoch to avoid boundary artifacts.
        rank: Union[int, None]=None, # numeric rank of the input
        verbose: Union[bool, int, str]=False # `mne.verbose` for details. Should only be passed as a keyword argument.
    ) -> (SimpleNamespace, SimpleNamespace): # The `features` with, e.g., `.pow`, `.cov` as attributes
                                             # and `info` outputs with `.foi` and `.n_valid_total` attributes.
    # Compute spectral features from complex Morlet Wavelet transform.
    if  not ('eeg' in inst or 'meg' in inst):
        raise ValueError('Currently only supporting EEG or MEG data.')

    if 'eeg' in inst and 'meg' in inst or 'mag' in inst and 'grad' in inst:
        raise ValueError('Currently only supporting unique sensor types at once. '
                         'Please pick your data types.')
   
    out = None
    sfreq = inst.info['sfreq']
    inst_copy_pick = inst.copy().pick(('eeg', 'meg'))
    if isinstance(inst, BaseRaw):
        data = inst_copy_pick.get_data()
    elif isinstance(inst, BaseEpochs):
        data = inst_copy_pick.get_data(copy=False)
    if isinstance(inst, BaseRaw) and nan_from_annotations:
        _set_nan_from_annotations_raw(inst, data, inst.annotations)
    elif isinstance(inst, BaseEpochs) and nan_from_annotations:
        raise ValueError('Converting bad annotations to NaN is only supported '
                         'for continous (raw) data')
    elif isinstance(inst, BaseEpochs):
        if prepend_nan_epochs:
            data = _prepand_nan_epochs(data)
        data = np.hstack(data)  # concatenate epochs

    out, info = compute_spectral_features_array(
        data=data, sfreq=sfreq, bw_oct=bw_oct, qt=qt, delta_oct=delta_oct,
        foi_start=foi_start, foi_end=foi_end, window_shift=window_shift,
        kernel_width=kernel_width, freq_shift_factor=freq_shift_factor,
        allow_fraction_nan=allow_fraction_nan,
        features=features, density=density,
        rank=rank,
        verbose=verbose
    )
    data_unit = ''
    if 'eeg' in inst:
        data_unit = 'V'
    elif 'mag' in inst:
        data_unit = 'T'
    elif 'grad' in inst:
        data_unit = 'T/cm'
    info.unit = f'{data_unit}²/{"Hz" if density == "Hz" else "oct"}'

    return out, info

# %% ../nbs/api/wavelets.ipynb 9
def spectrum_from_features(
        data: np.ndarray,  # spectral features, e.g. power, shape(n_channels, n_frequencies)
        freqs: np.ndarray, # frequencies, shape(n_frequencies)
        inst_info: mne.Info # the meta data of the MNE instance used for computing the features
    ) -> mne.time_frequency.Spectrum: # the MNE power spectrum object 
    """Create MNE averaged power spectrum object from features"""
    state = dict(
        method='morlet',
        data=data,
        sfreq=inst_info['sfreq'],
        dims=('channel', 'freq'),
        freqs=freqs,
        inst_type_str='Raw',
        data_type='Averaged EEG',
        info=inst_info,
    )
    defaults = dict(
        method=None, fmin=None, fmax=None, tmin=None, tmax=None,
        picks=None, proj=None, reject_by_annotation=None, n_jobs=None,
        verbose=None, remove_dc=None, exclude=None
    )
    return mne.time_frequency.Spectrum(state, **defaults)

# %% ../nbs/api/wavelets.ipynb 11
def ro_corrcoef(
        x: np.ndarray, # the seed (assuming time samples on last axis)
        y: np.ndarray, # the targets (assuming time samples on last axis)
        dim: int # number of dimensions
    ) -> SimpleNamespace: # the computed correlation values and statistics:
    # vectorized correlation coefficient and additional statistics.
    ax = dim - 1

    out = SimpleNamespace()
    # check if squeeze is needed
    out.r = ((np.mean(x * y, ax) - x.mean(ax) * y.mean(ax)) / 
             np.sqrt(np.mean(x ** 2, ax) - x.mean(ax) ** 2) /
             np.sqrt(np.mean(y ** 2, ax) - y.mean(ax) ** 2))

    n = x.shape[ax]
    dof = n - 2
    out.t = out.r * np.sqrt(dof / (1 - out.r ** 2))
    out.p = (1 - stats.t.cdf(np.abs(out.t), dof)) * 2
    out.p = np.clip(  # clip p-values to avoid zero-divions errors
        out.p, a_min=np.finfo(out.p.dtype).eps, a_max=None
    )
    out.z = np.sign(out.r) * stats.norm.ppf(1 - out.p / 2, 0, 1)
    out.r_f = out.r @ (1 + (1 - out.r ** 2) / (2 * n))
    out.r_op = out.r @ (1 + (1 - out.r ** 2) / (2 * (n - 3)))
    return out


# %% ../nbs/api/wavelets.ipynb 14
def bw2qt(
        bw: float, # the Wavelet's bandwidth
    ) -> float:  # characteristic Morlet parameter
    L = sqrt(2 * log(2))
    qt = (
        (2 ** (bw) + 2 ** (-bw) + 2) /
        (2 ** (bw) -2 ** (-bw)) * L
    )
    return qt

assert round(bw2qt(0.5), 1) == 6.9

# %% ../nbs/api/wavelets.ipynb 15
def qt2bw(
        qt: float, # characteristic Morlet parameter
    ) -> float:  # the Wavelet's bandwidth
    L = sqrt(2 * log(2))
    bw = log2(
        sqrt((L ** 2 / (L - qt) ** 2) - 
             ((L + qt) / (L - qt))) - 
        (L / (L - qt))
    )
    return bw

assert round(qt2bw(6.9), 1) == 0.5

# %% ../nbs/api/wavelets.ipynb 17
def plot_wavelet_family(
        wavelets: list, # List of wavelets and associated parameters.
        foi: np.ndarray, # Frequencies of interest.
        sampling_rate: float=1e3, #  Wavelet frequency. Inverse of the time separating two points. 
        cmap: mpl.colors.Colormap=plt.cm.viridis, # Colormap.
        f_scale: str="linear", # X-axis scale for the power spectra. 'log' | 'linear'.
        scale: Union[float, int]=4, # Window scaling factor. If <1 the wavelet will be cropped. If >1 wavelet will be padded with 0 leading to a smoother frequency domain representation.
        fmin: Union[float, int]=0, # Min frequency to display.
        fmax: Union[float, int]=120, # Max frequency to display,
    ) -> mpl.figure.Figure:

    fig, axes = plt.subplots(len(wavelets), 2, sharex="col")    
    axes = axes[::-1, :]
    colors = cmap(np.linspace(0.1, 0.9, len(wavelets)))
    for ax in axes[1:].flatten():
        ax.spines[['bottom', 'top', 'left', 'right']].set_visible(False)
        for tick in ax.xaxis.get_major_ticks():
            tick.tick1line.set_visible(False)
            tick.tick2line.set_visible(False)
    for ax in axes[:1].flatten():
        ax.spines[['top', 'left', 'right']].set_visible(False)

    for i, (w, *_) in enumerate(wavelets):
        axes[i, 0].plot(np.arange(len(w)) - len(w) / 2, w.real, color=colors[i])
        axes[i, 0].plot(
            np.arange(len(w)) - len(w) / 2, w.imag, color=colors[i], ls="--"
        )
        axes[i, 0].set_yticks([])

        w = w.ravel()
        xf = np.fft.fftshift(np.fft.fftfreq(int(len(w) * scale), 1 / sampling_rate))
        yf = np.fft.fftshift(np.fft.fft(w, n=int(len(w) * scale)))
        yf /= np.abs(yf).max()
        mask = (xf > fmin) & (xf < fmax)

        axes[i, 1].plot(xf[mask], np.abs(yf[mask]) ** 2, color=colors[i])
        axes[i, 1].set_yticks([])
        axes[i, 1].set_title(f"{foi[i]:.1f} Hz", y=0.1, x=-.1)

    axes[0, 0].set_xlabel("Time [ms]")
    axes[0, 1].set_xlabel("Frequency [Hz]")
    axes[0, 0].spines["bottom"].set_visible(True)
    axes[0, -1].spines["bottom"].set_visible(True)
    if f_scale == "log":
        axes[0, 1].semilogx(base=2)
        f2 = mpl.ticker.StrMethodFormatter("{x:.0f}")
        axes[0, 1].xaxis.set_major_formatter(f2)
    return axes
