# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math
import warnings
from typing import Optional, Tuple

import torch
from torch import Tensor

from torchmetrics.utilities.checks import _check_same_shape
from torchmetrics.utilities.imports import _FAST_BSS_EVAL_AVAILABLE, _TORCH_GREATER_EQUAL_1_8

# import or def the norm/solve function
if _TORCH_GREATER_EQUAL_1_8:
    from torch.linalg import norm

    solve = torch.linalg.solve
else:
    from torch import norm
    from torch import solve as _solve
    from torch.nn.functional import pad

    def solve(A: Tensor, b: Tensor) -> Tensor:
        return _solve(b[..., None], A)[0][..., 0]


if _FAST_BSS_EVAL_AVAILABLE and _TORCH_GREATER_EQUAL_1_8:
    from fast_bss_eval.torch.cgd import toeplitz_conjugate_gradient
else:
    toeplitz_conjugate_gradient = None


def _symmetric_toeplitz(vector: Tensor) -> Tensor:
    """Construct a symmetric Toeplitz matrix using one vector.

    Args:
        vector: shape [..., L]

    Example:
        >>> from torchmetrics.functional.audio.sdr import _symmetric_toeplitz
        >>> import torch
        >>> v = torch.tensor([0, 1, 2, 3, 4])
        >>> _symmetric_toeplitz(v)
        tensor([[0, 1, 2, 3, 4],
                [1, 0, 1, 2, 3],
                [2, 1, 0, 1, 2],
                [3, 2, 1, 0, 1],
                [4, 3, 2, 1, 0]])

    Returns:
        a symmetric Toeplitz matrix of shape [..., L, L]
    """
    vec_exp = torch.cat([torch.flip(vector, dims=(-1,)), vector[..., 1:]], dim=-1)
    v_len = vector.shape[-1]
    return torch.as_strided(
        vec_exp, size=vec_exp.shape[:-1] + (v_len, v_len), stride=vec_exp.stride()[:-1] + (1, 1)
    ).flip(dims=(-1,))


def _compute_autocorr_crosscorr(target: Tensor, preds: Tensor, corr_len: int) -> Tuple[Tensor, Tensor]:
    r"""Compute the auto correlation of `target` and the cross correlation of `target` and `preds` using the fast
    Fourier transform (FFT). Let's denotes the symmetric Toeplitz matric of the auto correlation of `target` as
    `R`, the cross correlation as 'b', then solving the equation `Rh=b` could have `h` as the coordinate of
    `preds` in the column space of the `corr_len` shifts of `target`.

    Args:
        target: the target (reference) signal of shape [..., time]
        preds: the preds (estimated) signal of shape [..., time]
        corr_len: the length of the auto correlation and cross correlation

    Returns:
        the auto correlation of `target` of shape [..., corr_len]
        the cross correlation of `target` and `preds` of shape [..., corr_len]
    """
    # the valid length for the signal after convolution
    n_fft = 2 ** math.ceil(math.log2(preds.shape[-1] + target.shape[-1] - 1))

    # computes the auto correlation of `target`
    # r_0 is the first row of the symmetric Toeplitz matric
    if _TORCH_GREATER_EQUAL_1_8:
        t_fft = torch.fft.rfft(target, n=n_fft, dim=-1)
        r_0 = torch.fft.irfft(t_fft.real**2 + t_fft.imag**2, n=n_fft)[..., :corr_len]
    else:
        t_pad = pad(target, (0, n_fft - target.shape[-1]), "constant", 0)
        t_fft = torch.rfft(t_pad, signal_ndim=1)
        real = t_fft[..., 0] ** 2 + t_fft[..., 1] ** 2
        imag = torch.zeros(real.shape, dtype=real.dtype, device=real.device)
        result = torch.stack([real, imag], len(real.shape))
        r_0 = torch.irfft(result, signal_ndim=1, signal_sizes=[n_fft])[..., :corr_len]

    # computes the cross-correlation of `target` and `preds`
    if _TORCH_GREATER_EQUAL_1_8:
        p_fft = torch.fft.rfft(preds, n=n_fft, dim=-1)
        b = torch.fft.irfft(t_fft.conj() * p_fft, n=n_fft, dim=-1)[..., :corr_len]
    else:
        p_pad = pad(preds, (0, n_fft - preds.shape[-1]), "constant", 0)
        p_fft = torch.rfft(p_pad, signal_ndim=1)
        real = t_fft[..., 0] * p_fft[..., 0] + t_fft[..., 1] * p_fft[..., 1]
        imag = t_fft[..., 0] * p_fft[..., 1] - t_fft[..., 1] * p_fft[..., 0]
        result = torch.stack([real, imag], len(real.shape))
        b = torch.irfft(result, signal_ndim=1, signal_sizes=[n_fft])[..., :corr_len]

    return r_0, b


def signal_distortion_ratio(
    preds: Tensor,
    target: Tensor,
    use_cg_iter: Optional[int] = None,
    filter_length: int = 512,
    zero_mean: bool = False,
    load_diag: Optional[float] = None,
) -> Tensor:
    r"""Signal to Distortion Ratio (SDR) [1,2]

    Args:
        preds: shape ``[..., time]``
        target: shape ``[..., time]``
        use_cg_iter:
            If provided, conjugate gradient descent is used to solve for the distortion
            filter coefficients instead of direct Gaussian elimination, which requires that
            ``fast-bss-eval`` is installed and pytorch version >= 1.8.
            This can speed up the computation of the metrics in case the filters
            are long. Using a value of 10 here has been shown to provide
            good accuracy in most cases and is sufficient when using this
            loss to train neural separation networks.
        filter_length: The length of the distortion filter allowed
        zero_mean: When set to True, the mean of all signals is subtracted prior to computation of the metrics
        load_diag:
            If provided, this small value is added to the diagonal coefficients of
            the system metrics when solving for the filter coefficients.
            This can help stabilize the metric in the case where some reference signals may sometimes be zero

    Returns:
        sdr value of shape ``[...]``

    Example:
        >>> from torchmetrics.functional.audio import signal_distortion_ratio
        >>> import torch
        >>> g = torch.manual_seed(1)
        >>> preds = torch.randn(8000)
        >>> target = torch.randn(8000)
        >>> signal_distortion_ratio(preds, target)
        tensor(-12.0589)
        >>> # use with permutation_invariant_training
        >>> from torchmetrics.functional.audio import permutation_invariant_training
        >>> preds = torch.randn(4, 2, 8000)  # [batch, spk, time]
        >>> target = torch.randn(4, 2, 8000)
        >>> best_metric, best_perm = permutation_invariant_training(preds, target, signal_distortion_ratio, 'max')
        >>> best_metric
        tensor([-11.6375, -11.4358, -11.7148, -11.6325])
        >>> best_perm
        tensor([[1, 0],
                [0, 1],
                [1, 0],
                [0, 1]])

    References:
        [1] Vincent, E., Gribonval, R., & Fevotte, C. (2006). Performance measurement in blind audio source separation.
        IEEE Transactions on Audio, Speech and Language Processing, 14(4), 1462–1469.

        [2] Scheibler, R. (2021). SDR -- Medium Rare with Fast Computations.
    """
    _check_same_shape(preds, target)

    # use double precision
    preds_dtype = preds.dtype
    preds = preds.double()
    target = target.double()

    if zero_mean:
        preds = preds - preds.mean(dim=-1, keepdim=True)
        target = target - target.mean(dim=-1, keepdim=True)

    # normalize along time-axis to make preds and target have unit norm
    target = target / torch.clamp(norm(target, dim=-1, keepdim=True), min=1e-6)
    preds = preds / torch.clamp(norm(preds, dim=-1, keepdim=True), min=1e-6)

    # solve for the optimal filter
    # compute auto-correlation and cross-correlation
    r_0, b = _compute_autocorr_crosscorr(target, preds, corr_len=filter_length)

    if load_diag is not None:
        # the diagonal factor of the Toeplitz matrix is the first coefficient of r_0
        r_0[..., 0] += load_diag

    if use_cg_iter is not None and _FAST_BSS_EVAL_AVAILABLE and _TORCH_GREATER_EQUAL_1_8:
        # use preconditioned conjugate gradient
        sol = toeplitz_conjugate_gradient(r_0, b, n_iter=use_cg_iter)
    else:
        if use_cg_iter is not None:
            if not _FAST_BSS_EVAL_AVAILABLE:
                warnings.warn(
                    "The `use_cg_iter` parameter of `SDR` requires that `fast-bss-eval` is installed. "
                    "To make this this warning disappear, you could install `fast-bss-eval` using "
                    "`pip install fast-bss-eval` or set `use_cg_iter=None`. For this time, the solver "
                    "provided by Pytorch is used.",
                    UserWarning,
                )
            elif not _TORCH_GREATER_EQUAL_1_8:
                warnings.warn(
                    "The `use_cg_iter` parameter of `SDR` requires a Pytorch version >= 1.8. "
                    "To make this this warning disappear, you could change to Pytorch v1.8+ or set `use_cg_iter=None`. "
                    "For this time, the solver provided by Pytorch is used.",
                    UserWarning,
                )
        # regular matrix solver
        r = _symmetric_toeplitz(r_0)  # the auto-correlation of the L shifts of `target`
        sol = solve(r, b)

    # compute the coherence
    coh = torch.einsum("...l,...l->...", b, sol)

    # transform to decibels
    ratio = coh / (1 - coh)
    val = 10.0 * torch.log10(ratio)

    if preds_dtype == torch.float64:
        return val
    else:
        return val.float()


def scale_invariant_signal_distortion_ratio(preds: Tensor, target: Tensor, zero_mean: bool = False) -> Tensor:
    """Calculates Scale-invariant signal-to-distortion ratio (SI-SDR) metric. The SI-SDR value is in general
    considered an overall measure of how good a source sound.

    Args:
        preds: shape ``[...,time]``
        target: shape ``[...,time]``
        zero_mean: If to zero mean target and preds or not

    Returns:
        si-sdr value of shape [...]

    Example:
        >>> from torchmetrics.functional.audio import scale_invariant_signal_distortion_ratio
        >>> target = torch.tensor([3.0, -0.5, 2.0, 7.0])
        >>> preds = torch.tensor([2.5, 0.0, 2.0, 8.0])
        >>> scale_invariant_signal_distortion_ratio(preds, target)
        tensor(18.4030)

    References:
        [1] Le Roux, Jonathan, et al. "SDR half-baked or well done." IEEE International Conference on Acoustics, Speech
        and Signal Processing (ICASSP) 2019.
    """
    _check_same_shape(preds, target)
    eps = torch.finfo(preds.dtype).eps

    if zero_mean:
        target = target - torch.mean(target, dim=-1, keepdim=True)
        preds = preds - torch.mean(preds, dim=-1, keepdim=True)

    alpha = (torch.sum(preds * target, dim=-1, keepdim=True) + eps) / (
        torch.sum(target**2, dim=-1, keepdim=True) + eps
    )
    target_scaled = alpha * target

    noise = target_scaled - preds

    val = (torch.sum(target_scaled**2, dim=-1) + eps) / (torch.sum(noise**2, dim=-1) + eps)
    val = 10 * torch.log10(val)

    return val
