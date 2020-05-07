# Copyright (c) 2019-2020, NVIDIA CORPORATION.
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

import cupy as cp
import warnings

from math import ceil

from .utils._caches import _cupy_kernel_cache, _numba_kernel_cache


def _pad_h(h, up):
    """Store coefficients in a transposed, flipped arrangement.
    For example, suppose upRate is 3, and the
    input number of coefficients is 10, represented as h[0], ..., h[9].
    Then the internal buffer will look like this::
       h[9], h[6], h[3], h[0],   // flipped phase 0 coefs
       0,    h[7], h[4], h[1],   // flipped phase 1 coefs (zero-padded)
       0,    h[8], h[5], h[2],   // flipped phase 2 coefs (zero-padded)
    """
    h_padlen = len(h) + (-len(h) % up)
    h_full = cp.zeros(h_padlen, h.dtype)
    h_full[: len(h)] = h
    h_full = h_full.reshape(-1, up).T[:, ::-1].ravel()
    return h_full


def _output_len(len_h, in_len, up, down):
    in_len_copy = in_len + (len_h + (-len_h % up)) // up - 1
    nt = in_len_copy * up
    need = nt // down
    if nt % down > 0:
        need += 1
    return need


class _cupy_upfirdn_wrapper(object):
    def __init__(self, grid, block, stream, kernel):
        if isinstance(grid, int):
            grid = (grid,)
        if isinstance(block, int):
            block = (block,)

        self.grid = grid
        self.block = block
        self.stream = stream
        self.kernel = kernel

    def __call__(
        self,
        x,
        h_trans_flip,
        up,
        down,
        axis,
        x_shape_a,
        h_per_phase,
        padded_len,
        out,
    ):

        kernel_args = (
            x,
            h_trans_flip,
            up,
            down,
            axis,
            x_shape_a,
            h_per_phase,
            padded_len,
            out,
            out.shape[0],
        )

        self.stream.use()
        self.kernel(self.grid, self.block, kernel_args)


class _cupy_upfirdn2d_wrapper(object):
    def __init__(self, grid, block, stream, kernel):
        if isinstance(grid, int):
            grid = (grid,)
        if isinstance(block, int):
            block = (block,)

        self.grid = grid
        self.block = block
        self.stream = stream
        self.kernel = kernel

    def __call__(
        self,
        x,
        h_trans_flip,
        up,
        down,
        axis,
        x_shape_a,
        h_per_phase,
        padded_len,
        out,
    ):

        kernel_args = (
            x,
            x.shape[1],
            h_trans_flip,
            up,
            down,
            axis,
            x_shape_a,
            h_per_phase,
            padded_len,
            out,
            out.shape[0],
            out.shape[1],
        )

        self.stream.use()
        self.kernel(self.grid, self.block, kernel_args)


def _get_backend_kernel(
    dtype, grid, block, stream, use_numba, k_type,
):
    from .utils._compile_kernels import _stream_cupy_to_numba, GPUKernel

    if not use_numba:
        kernel = _cupy_kernel_cache[(dtype.name, k_type.value)]
        if kernel:
            if k_type == GPUKernel.UPFIRDN:
                return _cupy_upfirdn_wrapper(grid, block, stream, kernel)
            elif k_type == GPUKernel.UPFIRDN2D:
                return _cupy_upfirdn2d_wrapper(grid, block, stream, kernel)
        else:
            raise ValueError(
                "Kernel {} not found in _cupy_kernel_cache".format(k_type)
            )

    else:
        warnings.warn(
            "Numba kernels will be removed in a later release",
            FutureWarning,
            stacklevel=4,
        )

        nb_stream = _stream_cupy_to_numba(stream)
        kernel = _numba_kernel_cache[(dtype.name, k_type.value)]
        if kernel:
            return kernel[grid, block, nb_stream]
        else:
            raise ValueError(
                "Kernel {} not found in _numba_kernel_cache".format(k_type)
            )

    raise NotImplementedError(
        "No kernel found for k_type {}, datatype {}".format(k_type, dtype.name)
    )


class _UpFIRDn(object):
    def __init__(self, h, x_dtype, up, down):
        """Helper for resampling"""
        h = cp.asarray(h)
        if h.ndim != 1 or h.size == 0:
            raise ValueError("h must be 1D with non-zero length")

        self._output_type = cp.result_type(h.dtype, x_dtype, cp.float32)
        h = cp.asarray(h, self._output_type)
        self._up = int(up)
        self._down = int(down)
        if self._up < 1 or self._down < 1:
            raise ValueError("Both up and down must be >= 1")
        # This both transposes, and "flips" each phase for filtering
        self._h_trans_flip = _pad_h(h, self._up)
        self._h_trans_flip = cp.ascontiguousarray(self._h_trans_flip)

    def apply_filter(
        self,
        x,
        axis,
        cp_stream,
        autosync,
        use_numba,
    ):
        """Apply the prepared filter to the specified axis of a nD signal x"""
        from .utils._compile_kernels import _populate_kernel_cache, GPUKernel

        output_len = _output_len(
            len(self._h_trans_flip), x.shape[axis], self._up, self._down
        )
        output_shape = cp.asarray(x.shape)
        output_shape[axis] = output_len
        out = cp.zeros(
            cp.asnumpy(output_shape), dtype=self._output_type, order="C"
        )
        axis = axis % x.ndim

        # Precompute variables on CPU
        x_shape_a = x.shape[axis]
        h_per_phase = len(self._h_trans_flip) // self._up
        padded_len = x.shape[axis] + (len(self._h_trans_flip) // self._up) - 1

        if out.ndim > 1:
            threadsperblock = (16, 16)
            blockspergrid_x = ceil(out.shape[0] / threadsperblock[0])
            blockspergrid_y = ceil(out.shape[1] / threadsperblock[1])
            blockspergrid = (blockspergrid_x, blockspergrid_y)

        else:
            device_id = cp.cuda.Device()
            numSM = device_id.attributes["MultiProcessorCount"]
            blockspergrid = numSM * 20
            threadsperblock = 512

        if out.ndim == 1:
            _populate_kernel_cache(
                out.dtype.type, use_numba, GPUKernel.UPFIRDN
            )
            kernel = _get_backend_kernel(
                out.dtype,
                blockspergrid,
                threadsperblock,
                cp_stream,
                use_numba,
                GPUKernel.UPFIRDN,
            )
        elif out.ndim == 2:
            _populate_kernel_cache(
                out.dtype.type, use_numba, GPUKernel.UPFIRDN2D
            )
            kernel = _get_backend_kernel(
                out.dtype,
                blockspergrid,
                threadsperblock,
                cp_stream,
                use_numba,
                GPUKernel.UPFIRDN2D,
            )
        else:
            raise NotImplementedError("upfirdn() requires ndim <= 2")

        kernel(
            cp.asarray(x, self._output_type),
            self._h_trans_flip,
            self._up,
            self._down,
            axis,
            x_shape_a,
            h_per_phase,
            padded_len,
            out,
        )

        if autosync is True:
            cp_stream.synchronize()

        return out
