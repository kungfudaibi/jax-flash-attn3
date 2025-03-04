from functools import partial

from . import _jax_flash_attn

import jax
import jax.numpy as jnp
from jax import core, dtypes
from jax.core import ShapedArray
from jax.interpreters import xla
from jax.lib import xla_client

from jax.interpreters import mlir
from jax.interpreters.mlir import ir
from jaxlib.hlo_helpers import custom_call
from jax.experimental import shard_map
_run_mha_fwd = core.Primitive("run_mha_fwd")
_run_mha_fwd.multiple_results = True
_run_mha_fwd.def_impl(partial(xla.apply_primitive, _run_mha_fwd))

_run_mha_bwd = core.Primitive("run_mha_bwd")
_run_mha_bwd.multiple_results = True
_run_mha_bwd.def_impl(partial(xla.apply_primitive, _run_mha_bwd))


def run_mha_fwd(q, k, v, is_causal=False, softmax_scale=1.0, softcap=0.0):
    tiled = jnp.array(0, dtype=jnp.int32)
    output, softmax_lse = _run_mha_fwd.bind(
        q,
        k,
        v,
        tiled,
        is_causal=is_causal,
        softmax_scale=softmax_scale,
        softcap=softcap,
    )
    return output, (output, softmax_lse, q, k, v)


def run_mha_bwd(is_causal, softmax_scale, softcap, res, grad):
    output, softmax_lse, q, k, v = res
    _b_sz, seqlen_q, num_heads, _head_size = q.shape
    b_sz, seqlen_k, num_heads_k, head_size = k.shape
    grad_q, grad_k, grad_v, softmax_d, dq_accum, softmax_lse_log2, dq_semaphore = (
        _run_mha_bwd.bind(
            grad,
            output,
            softmax_lse,
            q,
            k,
            v,
            is_causal=is_causal,
            softmax_scale=softmax_scale,
            softcap=softcap,
        )
    )
    if num_heads != num_heads_k:
        # MQA / GQA handling.
        _shape = b_sz, seqlen_k, num_heads_k, num_heads // num_heads_k, head_size
        grad_k = grad_k.reshape(_shape)
        grad_v = grad_v.reshape(_shape)
        grad_k = grad_k.sum(3)
        grad_v = grad_v.sum(3)
    return grad_q, grad_k, grad_v


@partial(jax.custom_vjp, nondiff_argnums=(3, 4, 5))
def run_mha(q, k, v, is_causal=False, softmax_scale=1.0, softcap=0.0):
    output, _ = run_mha_fwd(
        q, k, v, is_causal=is_causal, softmax_scale=softmax_scale, softcap=softcap
    )
    return output


# jax.config.update("experimental_xmap_spmd_lowering", True)
# jax.config.update("experimental_xmap_spmd_lowering_manual", True)


# def xmap_run_mha(q, k, v, is_causal, softmax_scale, softcap, device_count):
#     q_reshaped = q.reshape(device_count, q.shape[0] // device_count, *q.shape[1:])
#     k_reshaped = k.reshape(device_count, k.shape[0] // device_count, *k.shape[1:])
#     v_reshaped = v.reshape(device_count, v.shape[0] // device_count, *v.shape[1:])
#     xmapped = xmap(
#         partial(
#             run_mha, is_causal=is_causal, softmax_scale=softmax_scale, softcap=softcap
#         ),
#         in_axes=(
#             ("q", None, None, None, None),
#             ("q", None, None, None, None),
#             ("q", None, None, None, None),
#         ),
#         out_axes=("q", None, None, None, None),
#         axis_resources={"q": "q"},
#     )
#     out_reshaped = xmapped(q_reshaped, k_reshaped, v_reshaped)
#     return out_reshaped.reshape(q.shape)
def xmap_run_mha(q, k, v, is_causal, softmax_scale, softcap, device_count):
    mesh = jax.sharding.Mesh(jax.devices(), ('q',))
    
    q_reshaped = q.reshape(device_count, q.shape[0] // device_count, *q.shape[1:])
    k_reshaped = k.reshape(device_count, k.shape[0] // device_count, *k.shape[1:])
    v_reshaped = v.reshape(device_count, v.shape[0] // device_count, *v.shape[1:])

    mapped = shard_map.shard_map(
        partial(run_mha, is_causal=is_causal, softmax_scale=softmax_scale, softcap=softcap),
        mesh,
        in_specs=("q", None, None, None, None),
        out_specs=("q", None, None, None, None),
        check_rep=True
    )
    out_reshaped = mapped(q_reshaped, k_reshaped, v_reshaped)
    return out_reshaped.reshape(q.shape)


run_mha.defvjp(run_mha_fwd, run_mha_bwd)


def default_layouts(*shapes):
    return [range(len(shape) - 1, -1, -1) for shape in shapes]


def round_multiple(x, m):
    return (x + m - 1) // m * m


def _run_mha_fwd_cuda_lowering(ctx, q, k, v, tiled, is_causal, softmax_scale, softcap):
    q_type = ir.RankedTensorType(q.type)
    k_type = ir.RankedTensorType(k.type)
    v_type = ir.RankedTensorType(v.type)
    tiled_type = ir.RankedTensorType(tiled.type)

    if q_type.element_type not in [ir.F16Type.get(), ir.BF16Type.get()]:
        raise ValueError(f"only f16/bf16 is supported {q_type.element_type}")

    is_bf16 = q_type.element_type == ir.BF16Type.get()

    for dt in [q_type, k_type, v_type]:
        if dt.element_type == q_type.element_type:
            continue
        raise ValueError(
            f"incoherent element types {dt.element_type} {q_type.element_type}"
        )

    b_sz, seqlen_q, num_heads, head_size_og = q_type.shape
    _b_sz, seqlen_k, num_heads_k, _head_size_og = k_type.shape
    n_softmax_lse = b_sz * num_heads * seqlen_q

    expected_kv = [b_sz, seqlen_k, num_heads_k, head_size_og]
    if expected_kv != k_type.shape:
        raise ValueError(f"unexpected key shape {k_type.shape}, exp {expected_kv}")
    if expected_kv != v_type.shape:
        raise ValueError(f"unexpected value shape {v_type.shape}, exp {expected_kv}")
    if num_heads % num_heads_k != 0:
        print(
            f"num_heads has to be divisible by num_heads_k ({num_heads}, {num_heads_k})"
        )

    if head_size_og > 256:
        raise ValueError(f"only supports head dim at most 256, got {head_size_og}")
    if head_size_og % 8 != 0:
        raise ValueError(f"only supports head dim divisible by 8, got {head_size_og}")

    head_size = round_multiple(head_size_og, 8)
    head_size_rounded = round_multiple(head_size, 32)
    seqlen_q_rounded = round_multiple(seqlen_q, 128)
    seqlen_k_rounded = round_multiple(seqlen_k, 128)
    window_size_left = -1
    window_size_right = 0 if is_causal else -1

    opaque = _jax_flash_attn.create_params(
        seqlen_q * num_heads * head_size_og,  # q_batch_stride,
        seqlen_k * num_heads_k * head_size_og,  # k_batch_stride,
        seqlen_k * num_heads_k * head_size_og,  # v_batch_stride,
        seqlen_q * num_heads * head_size_og,  # o_batch_stride,
        num_heads * head_size_og,  # q_row_stride,
        num_heads_k * head_size_og,  # k_row_stride,
        num_heads_k * head_size_og,  # v_row_stride,
        num_heads * head_size_og,  # o_row_stride,
        head_size_og,  # q_head_stride,
        head_size_og,  # k_head_stride,
        head_size_og,  # v_head_stride,
        head_size_og,  # o_head_stride,
        b_sz,  # b,
        num_heads,  # h,
        num_heads_k,  # h_k,
        head_size,  # d,
        head_size_rounded,  # d_rounded,
        softmax_scale,  # softmax_scale,
        softcap,
        seqlen_q,
        seqlen_k,
        seqlen_q_rounded,
        seqlen_k_rounded,
        window_size_left,
        window_size_right,
        int(is_causal),  # is_causal,
        int(is_bf16),  # is_bf16
    )

    out = custom_call(
        b"run_mha_fwd",
        result_types=[
            ir.RankedTensorType.get(q_type.shape, q_type.element_type),
            ir.RankedTensorType.get((n_softmax_lse,), ir.F32Type.get()),
        ],
        operands=[q, k, v, tiled],
        backend_config=opaque,
        operand_layouts=default_layouts(
            q_type.shape, k_type.shape, v_type.shape, tiled_type.shape
        ),
        result_layouts=default_layouts(q_type.shape, (n_softmax_lse,)),
    )
    return out.results[:2]


def _run_mha_bwd_cuda_lowering(
    ctx,
    grad,
    output,
    softmax_lse,
    q,
    k,
    v,
    is_causal=False,
    softmax_scale=1.0,
    softcap=0.0,
):
    q_type = ir.RankedTensorType(q.type)
    k_type = ir.RankedTensorType(k.type)
    v_type = ir.RankedTensorType(v.type)
    q_shape = q_type.shape
    k_shape = k_type.shape
    v_shape = v_type.shape

    is_bf16 = q_type.element_type == ir.BF16Type.get()
    b_sz, seqlen_q, num_heads, head_size_og = q_type.shape
    _b_sz, seqlen_k, num_heads_k, _head_size_og = k_type.shape
    n_softmax_lse = b_sz * num_heads * seqlen_q

    expected_kv = [b_sz, seqlen_k, num_heads_k, head_size_og]
    if expected_kv != k_type.shape:
        raise ValueError(f"unexpected key shape {k_type.shape}, exp {expected_kv}")
    if expected_kv != v_type.shape:
        raise ValueError(f"unexpected value shape {v_type.shape}, exp {expected_kv}")

    if num_heads % num_heads_k != 0:
        print(
            f"num_heads has to be divisible by num_heads_k ({num_heads}, {num_heads_k})"
        )

    if head_size_og > 256:
        raise ValueError(f"only supports head dim at most 256, got {head_size_og}")
    if head_size_og % 8 != 0:
        raise ValueError(f"only supports head dim divisible by 8, got {head_size_og}")

    head_size = round_multiple(head_size_og, 8)
    head_size_rounded = 64 if head_size <= 64 else round_multiple(head_size, 32)
    k_block_m = 128 if head_size <= 64 else (64 if head_size < 256 else 32)
    seqlen_q_rounded = round_multiple(seqlen_q, k_block_m)
    seqlen_k_rounded = round_multiple(seqlen_k, 128)
    window_size_left = -1
    window_size_right = 0 if is_causal else -1

    opaque = _jax_flash_attn.create_params(
        seqlen_q * num_heads * head_size_og,  # q_batch_stride,
        seqlen_k * num_heads_k * head_size_og,  # k_batch_stride,
        seqlen_k * num_heads_k * head_size_og,  # v_batch_stride,
        seqlen_q * num_heads * head_size_og,  # o_batch_stride,
        num_heads * head_size_og,  # q_row_stride,
        num_heads_k * head_size_og,  # k_row_stride,
        num_heads_k * head_size_og,  # v_row_stride,
        num_heads * head_size_og,  # o_row_stride,
        head_size_og,  # q_head_stride,
        head_size_og,  # k_head_stride,
        head_size_og,  # v_head_stride,
        head_size_og,  # o_head_stride,
        b_sz,  # b,
        num_heads,  # h,
        num_heads_k,  # h_k,
        head_size,  # d,
        head_size_rounded,  # d_rounded,
        softmax_scale,  # softmax_scale,
        softcap,
        seqlen_q,
        seqlen_k,
        seqlen_q_rounded,
        seqlen_k_rounded,
        window_size_left,
        window_size_right,
        int(is_causal),  # is_causal,
        int(is_bf16),  # is_bf16
    )

    softmax_d_shape = b_sz, num_heads, seqlen_q_rounded
    dq_accum_shape = b_sz, seqlen_q_rounded, num_heads, head_size_rounded
    dq_semaphore_shape = seqlen_q_rounded, b_sz, num_heads

    dq_shape = q_shape
    if num_heads_k != num_heads:
        # MQA / GQA handling.
        # These are named dk_expanded and dv_expanded in flash_attn/flash_api.cpp.
        dk_shape = b_sz, seqlen_k, num_heads, head_size
        dv_shape = b_sz, seqlen_k, num_heads, head_size
    else:
        dk_shape = k_shape
        dv_shape = v_shape

    out = custom_call(
        b"run_mha_bwd",
        result_types=[
            ir.RankedTensorType.get(dq_shape, q_type.element_type),
            ir.RankedTensorType.get(dk_shape, k_type.element_type),
            ir.RankedTensorType.get(dv_shape, v_type.element_type),
            ir.RankedTensorType.get(softmax_d_shape, ir.F32Type.get()),
            ir.RankedTensorType.get(dq_accum_shape, ir.F32Type.get()),
            ir.RankedTensorType.get(softmax_d_shape, ir.F32Type.get()),
            ir.RankedTensorType.get(dq_semaphore_shape, ir.F32Type.get()),
        ],
        operands=[grad, output, softmax_lse, q, k, v],
        backend_config=opaque,
        operand_layouts=default_layouts(
            q_shape,
            q_shape,
            (n_softmax_lse,),
            q_shape,
            k_shape,
            v_shape,
        ),
        result_layouts=default_layouts(
            dq_shape,
            dk_shape,
            dv_shape,
            softmax_d_shape,
            dq_accum_shape,
            softmax_d_shape,
            dq_semaphore_shape,
        ),
    )
    # These return the expanded versions of the dk/dv gradients. These are aggregated in the bwd call.
    # It would be nicer to make this here but not sure how to handle this at the lowered level.
    return out.results


def _run_mha_fwd_abstract(q, k, v, tiled, is_causal, softmax_scale, softcap):
    q_dtype = dtypes.canonicalize_dtype(q.dtype)
    k_dtype = dtypes.canonicalize_dtype(k.dtype)
    v_dtype = dtypes.canonicalize_dtype(v.dtype)
    for dt in [q_dtype, k_dtype, v_dtype]:
        if dt in [jnp.float16, jnp.bfloat16]:
            continue
        raise ValueError(f"only f16/bf16 are supported {dt}")
    b_sz, seqlen_q, num_heads, head_size_og = q.shape
    return (
        ShapedArray(q.shape, q_dtype),  # output
        ShapedArray(
            (b_sz * num_heads * seqlen_q,), jnp.float32
        ),  # invvar
    )


def _run_mha_bwd_abstract(
    grad,
    output,
    softmax_lse,
    q,
    k,
    v,
    is_causal=False,
    softmax_scale=1.0,
    softcap=0.0,
):
    q_dtype = dtypes.canonicalize_dtype(q.dtype)
    k_dtype = dtypes.canonicalize_dtype(k.dtype)
    v_dtype = dtypes.canonicalize_dtype(v.dtype)

    b_sz, seqlen_q, num_heads, head_size_og = q.shape
    head_size = round_multiple(head_size_og, 8)
    head_size_rounded = 64 if head_size <= 64 else round_multiple(head_size, 32)
    k_block_m = 128 if head_size <= 64 else (64 if head_size < 256 else 32)
    seqlen_q_rounded = round_multiple(seqlen_q, k_block_m)
    seqlen_q_rounded = round_multiple(seqlen_q, 128)
    softmax_d_shape = b_sz, num_heads, seqlen_q_rounded

    dq_accum_shape = b_sz, seqlen_q_rounded, num_heads, head_size_rounded
    dq_semaphore_shape = seqlen_q_rounded, b_sz, num_heads
    return (
        ShapedArray(q.shape, q_dtype),  # grad q
        ShapedArray(k.shape, k_dtype),  # grad k
        ShapedArray(v.shape, v_dtype),  # grad v
        ShapedArray(softmax_d_shape, jnp.float32),
        ShapedArray(dq_accum_shape, jnp.float32),
        ShapedArray(softmax_d_shape, jnp.float32),
        ShapedArray(dq_semaphore_shape, jnp.float32),
    )


def _register():
    for _name, _value in _jax_flash_attn.get_flash_attn_registrations().items():
        xla_client.register_custom_call_target(_name, _value, platform="gpu")

    mlir.register_lowering(
        _run_mha_fwd,
        _run_mha_fwd_cuda_lowering,
        platform="gpu",
    )

    mlir.register_lowering(
        _run_mha_bwd,
        _run_mha_bwd_cuda_lowering,
        platform="gpu",
    )

    _run_mha_fwd.def_abstract_eval(_run_mha_fwd_abstract)

    _run_mha_bwd.def_abstract_eval(_run_mha_bwd_abstract)


_register()