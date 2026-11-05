// SPDX-License-Identifier: ISC
// Minimal stand-in for Chowdhury-DSP/math_approx, which Open303's diode filter includes. Touch-Seq Acid uses
// the TeeBee filter, so a plain std::tanh is all this needs to provide.
#pragma once

#include <cmath>

namespace math_approx {
template <int N = 7>
inline float tanh(float x) noexcept {
    return std::tanh(x);
}
}  // namespace math_approx
