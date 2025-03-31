from typing import Literal, NamedTuple

import torch
import torch.nn.functional as F


class EncoderOutput(NamedTuple):
    top_acts: torch.Tensor
    """Activations of the top-k latents."""

    top_indices: torch.Tensor
    """Indices of the top-k features."""

    pre_acts: torch.Tensor
    """Activations before the top-k selection."""

class QuantizedFusedEncoder(torch.autograd.Function):
    @staticmethod
    def forward(
        ctx, input, weight, bias, k: int, activation: Literal["groupmax", "topk"], 
        min_val: int, max_val: int, levels : int
    ):
        """
        input:   (N, D)
        weight:  (M, D)
        bias:    (M,)
        k:       int (number of top elements to select along dim=1)
        min_val: minimum value for quantization
        max_val: maximum value for quantization
        levels:  number of quantization levels
        """
        # Quantize using relu6 for now but might switch to hardtanh later
        out = F.relu6(F.linear(input, weight, bias))
        # Convert to continuous levels between 0 and levels-1
        x_scaled = (out - min_val) * (levels - 1) / (max_val - min_val)
		# Get lower discrete level
        x_floor = torch.floor(x_scaled)
		# Calculate probability of rounding up
        prob_up = x_scaled - x_floor
		# Generate random values for stochastic rounding
        rand = torch.rand_like(x_scaled)
		# Round up where random value is less than probability
        x_rounded = torch.where(rand < prob_up, x_floor + 1, x_floor)
		# Scale back to original range
        preacts = x_rounded * (max_val - min_val) / (levels - 1) + min_val 
        # Get top-k values and indices for each row
        if activation == "topk":
            values, indices = torch.topk(preacts, k, dim=1, sorted=False)
        elif activation == "groupmax":
            values, indices = preacts.unflatten(-1, (k, -1)).max(dim=-1)

            # torch.max gives us indices into each group, but we want indices into the
            # flattened tensor. Add the offsets to get the correct indices.
            num_latents = preacts.shape[1]
            offsets = torch.arange(
                0, num_latents, num_latents // k, device=preacts.device
            )
            indices = offsets + indices
        else:
            raise ValueError(f"Unknown activation: {activation}")

        # Save tensors needed for the backward pass
        ctx.save_for_backward(input, weight, bias, indices)
        ctx.k = k
        return values, indices, preacts

    @staticmethod
    def backward(ctx, grad_values, grad_indices, grad_preacts):
        input, weight, bias, indices = ctx.saved_tensors
        grad_input = grad_weight = grad_bias = None
        # --- Grad w.r.t. input ---
        if ctx.needs_input_grad[0]:
            grad_input = F.embedding_bag(
                indices,
                weight,
                mode="sum",
                per_sample_weights=grad_values.type_as(weight),
            )

        # --- Grad w.r.t. weight ---
        if ctx.needs_input_grad[1]:
            grad_weight = torch.zeros_like(weight)
            # Compute contributions from each top-k element:
            # computed as grad_values * input for each top-k location.
            contributions = grad_values.unsqueeze(2) * input.unsqueeze(1)
            _, _, D = contributions.shape
            # Flatten contributions to shape (N*k, D)
            contributions = contributions.reshape(-1, D)

            # Accumulate contributions into the correct rows of grad_weight.
            grad_weight.index_add_(0, indices.flatten(), contributions.type_as(weight))

        # --- Grad w.r.t. bias ---
        if bias is not None and ctx.needs_input_grad[2]:
            grad_bias = torch.zeros_like(bias)
            grad_bias.index_add_(
                0, indices.flatten(), grad_values.flatten().type_as(bias)
            )

        # The k parameter is an int, so return None for its gradient.
        return grad_input, grad_weight, grad_bias, None, None, None, None, None

class FusedEncoder(torch.autograd.Function):
    @staticmethod
    def forward(
        ctx, input, weight, bias, k: int, activation: Literal["groupmax", "topk"]
    ):
        """
        input:  (N, D)
        weight: (M, D)
        bias:   (M,)
        k:      int (number of top elements to select along dim=1)
        """
        preacts = F.relu(F.linear(input, weight, bias))

        # Get top-k values and indices for each row
        if activation == "topk":
            values, indices = torch.topk(preacts, k, dim=1, sorted=False)
        elif activation == "groupmax":
            values, indices = preacts.unflatten(-1, (k, -1)).max(dim=-1)

            # torch.max gives us indices into each group, but we want indices into the
            # flattened tensor. Add the offsets to get the correct indices.
            num_latents = preacts.shape[1]
            offsets = torch.arange(
                0, num_latents, num_latents // k, device=preacts.device
            )
            indices = offsets + indices
        else:
            raise ValueError(f"Unknown activation: {activation}")

        # Save tensors needed for the backward pass
        ctx.save_for_backward(input, weight, bias, indices)
        ctx.k = k
        return values, indices, preacts

    @staticmethod
    def backward(ctx, grad_values, grad_indices, grad_preacts):
        input, weight, bias, indices = ctx.saved_tensors
        grad_input = grad_weight = grad_bias = None

        # --- Grad w.r.t. input ---
        if ctx.needs_input_grad[0]:
            grad_input = F.embedding_bag(
                indices,
                weight,
                mode="sum",
                per_sample_weights=grad_values.type_as(weight),
            )

        # --- Grad w.r.t. weight ---
        if ctx.needs_input_grad[1]:
            grad_weight = torch.zeros_like(weight)
            # Compute contributions from each top-k element:
            # computed as grad_values * input for each top-k location.
            contributions = grad_values.unsqueeze(2) * input.unsqueeze(1)
            _, _, D = contributions.shape
            # Flatten contributions to shape (N*k, D)
            contributions = contributions.reshape(-1, D)

            # Accumulate contributions into the correct rows of grad_weight.
            grad_weight.index_add_(0, indices.flatten(), contributions.type_as(weight))

        # --- Grad w.r.t. bias ---
        if bias is not None and ctx.needs_input_grad[2]:
            grad_bias = torch.zeros_like(bias)
            grad_bias.index_add_(
                0, indices.flatten(), grad_values.flatten().type_as(bias)
            )

        # The k parameter is an int, so return None for its gradient.
        return grad_input, grad_weight, grad_bias, None, None


def fused_encoder(
    input,
    weight,
    bias,
    k: int,
    activation: Literal["groupmax", "topk"],
    quantization: bool, 
    min_val: int, 
    max_val: int, 
    levels : int
) -> EncoderOutput:
    """
    Convenience wrapper that performs an nn.Linear followed by `activation` with
    a backward pass optimized using index_add.
    """
    if not quantization:
        return EncoderOutput(
            *FusedEncoder.apply(input, weight, bias, k, activation)  # type: ignore
            )
    else:
        return EncoderOutput(
            *QuantizedFusedEncoder.apply(input, weight, bias, k, activation, 
                                         min_val, max_val, levels)  # type: ignore
            )
    # type: ignore
