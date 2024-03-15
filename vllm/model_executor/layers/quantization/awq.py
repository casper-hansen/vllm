from typing import Any, Dict, List, Optional

import torch
from torch.nn.parameter import Parameter

from vllm._C import ops
from vllm.model_executor.layers.linear import (LinearMethodBase,
                                               set_weight_attrs)
from vllm.model_executor.layers.quantization.base_config import QuantizationConfig


def make_divisible(c, divisor):
    return (c + divisor - 1) // divisor


def calculate_zeros_width(in_features, group_size=128, pack_num=8):
    if group_size >= 128:
        size_multiplier = 1
    elif group_size == 64:
        size_multiplier = 2
    elif group_size == 32:
        size_multiplier = 4
    else:
        raise NotImplementedError

    base_width = make_divisible(in_features // group_size, pack_num)
    base_width = make_divisible(base_width, size_multiplier) * size_multiplier
    return base_width


class AWQConfig(QuantizationConfig):
    """Config class for AWQ.

    Reference: https://arxiv.org/abs/2306.00978
    """

    def __init__(
        self,
        weight_bits: int,
        group_size: int,
        zero_point: bool,
        version: str
    ) -> None:
        self.weight_bits = weight_bits
        self.group_size = group_size
        self.zero_point = zero_point
        self.version = version

        if self.weight_bits != 4:
            raise ValueError(
                "Currently, only 4-bit weight quantization is supported for "
                f"AWQ, but got {self.weight_bits} bits.")
        self.pack_factor_int32 = 32 // self.weight_bits
        self.pack_factor_int16 = 16 // self.weight_bits
        
        self.interleave = 4

    def __repr__(self) -> str:
        return (f"AWQConfig(weight_bits={self.weight_bits}, "
                f"group_size={self.group_size}, "
                f"zero_point={self.zero_point})")

    def get_name(self) -> str:
        return "awq"

    def get_supported_act_dtypes(self) -> List[torch.dtype]:
        return [torch.half]

    def get_min_capability(self) -> int:
        # The AWQ kernel only supports Turing or newer GPUs.
        return 75

    @staticmethod
    def get_config_filenames() -> List[str]:
        return [
            "quant_config.json",  # E.g., casperhansen/vicuna-7b-v1.5-awq
            "quantize_config.json",  # E.g., abhinavkulkarni/mosaicml-mpt-7b-instruct-w4-g128-awq
        ]

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "AWQConfig":
        weight_bits = cls.get_from_keys(config, ["w_bit", "bits"])
        group_size = cls.get_from_keys(config, ["q_group_size", "group_size"])
        zero_point = cls.get_from_keys(config, ["zero_point"])
        version = cls.get_from_keys(config, ["version"])
        return cls(weight_bits, group_size, zero_point, version)

    def get_linear_method(self) -> "AWQLinearMethod":
        return AWQLinearMethod(self)

    def get_scaled_act_names(self) -> List[str]:
        return ["gelu", "gelu_fast", "gelu_new", "gelu_pytorch_tanh"]


class AWQLinearMethod(LinearMethodBase):
    """Linear method for AWQ.

    Args:
        quant_config: The AWQ quantization config.
    """

    def __init__(self, quant_config: AWQConfig):
        self.quant_config = quant_config

    def create_weights(self, input_size_per_partition: int,
                       output_size_per_partition: int, input_size: int,
                       output_size: int,
                       params_dtype: torch.dtype) -> Dict[str, Any]:
        if input_size_per_partition % self.quant_config.group_size != 0:
            raise ValueError(
                "The input size is not aligned with the quantized "
                "weight shape. This can be caused by too large "
                "tensor parallel size.")
        if output_size_per_partition % self.quant_config.pack_factor_int32 != 0:
            raise ValueError(
                "The output size is not aligned with the quantized "
                "weight shape. This can be caused by too large "
                "tensor parallel size.")

        if self.quant_config.version == "gemv_fast":
            qweight = Parameter(
                torch.empty(
                    output_size_per_partition // self.quant_config.interleave,
                    input_size_per_partition // self.quant_config.pack_factor_int16 * self.quant_config.interleave,
                    dtype=torch.int16,
                ),
                requires_grad=False,
            )
            set_weight_attrs(
                qweight, {
                    "input_dim": 1,
                    "output_dim": 0,
                    "packed_dim": 1,
                    "pack_factor": self.quant_config.pack_factor_int16,
                    "awq_interleave": self.quant_config.interleave,
                })
            qzeros = Parameter(
                torch.empty(
                    calculate_zeros_width(input_size_per_partition, self.quant_config.group_size) * self.quant_config.pack_factor_int32,
                    output_size_per_partition,
                    dtype=params_dtype,
                ),
                requires_grad=False,
            )
            set_weight_attrs(
                qzeros, {
                    "input_dim": 0,
                    "output_dim": 1,
                    "packed_dim": 0,
                })
            scales = Parameter(
                torch.empty(
                    calculate_zeros_width(input_size_per_partition, self.quant_config.group_size) * self.quant_config.pack_factor_int32,
                    output_size_per_partition,
                    dtype=params_dtype,
                ),
                requires_grad=False,
            )
            set_weight_attrs(
                scales, {
                    "input_dim": 0,
                    "output_dim": 1,
                    "packed_dim": 0,
                })
        else:
            qweight = Parameter(
                torch.empty(
                    input_size_per_partition,
                    output_size_per_partition // self.quant_config.pack_factor_int32,
                    dtype=torch.int32,
                ),
                requires_grad=False,
            )
            set_weight_attrs(
                qweight, {
                    "input_dim": 0,
                    "output_dim": 1,
                    "packed_dim": 1,
                    "pack_factor": self.quant_config.pack_factor_int32,
                })
            qzeros = Parameter(
                torch.empty(
                    input_size_per_partition // self.quant_config.group_size,
                    output_size_per_partition // self.quant_config.pack_factor_int32,
                    dtype=torch.int32,
                ),
                requires_grad=False,
            )
            set_weight_attrs(
                qzeros, {
                    "input_dim": 0,
                    "output_dim": 1,
                    "packed_dim": 1,
                    "pack_factor": self.quant_config.pack_factor_int32,
                })
            scales = Parameter(
                torch.empty(
                    input_size_per_partition // self.quant_config.group_size,
                    output_size_per_partition,
                    dtype=params_dtype,
                ),
                requires_grad=False,
            )
            set_weight_attrs(scales, {
                "input_dim": 0,
                "output_dim": 1,
            })
        return {
            "qweight": qweight,
            "qzeros": qzeros,
            "scales": scales,
        }

    def apply_weights(self,
                      weights: Dict[str, Any],
                      x: torch.Tensor,
                      bias: Optional[torch.Tensor] = None) -> torch.Tensor:
        qweight = weights["qweight"]
        scales = weights["scales"]
        qzeros = weights["qzeros"]
        reshaped_x = x.reshape(-1, x.shape[-1])
        if self.quant_config.version == "gemv_fast":
            out_shape = (x.shape[:-1] + (qweight.shape[0] * self.quant_config.interleave, ))
            GEMM_HEURISTIC_CONDITION = x.shape[:-1].numel() >= 8
            if not GEMM_HEURISTIC_CONDITION:
                out = ops.awq_gemv_fast(
                    reshaped_x, qweight, scales, qzeros, reshaped_x.shape[0],
                    out_shape[-1], reshaped_x.shape[1], self.quant_config.group_size
                )
            else:
                out = ops.awq_gemm_fast(reshaped_x, qweight, scales, qzeros)
        else:
            pack_factor = self.quant_config.pack_factor_int32
            out_shape = (x.shape[:-1] + (qweight.shape[-1] * pack_factor, ))

            # num_tokens >= threshold
            FP16_MATMUL_HEURISTIC_CONDITION = x.shape[:-1].numel() >= 256

            if FP16_MATMUL_HEURISTIC_CONDITION:
                out = ops.awq_dequantize(qweight, scales, qzeros, 0, 0, 0)
                out = torch.matmul(reshaped_x, out)
            else:
                out = ops.awq_gemm(reshaped_x, qweight, scales, qzeros,
                                   pack_factor)
        if bias is not None:
            out = out + bias
        return out.reshape(out_shape)
