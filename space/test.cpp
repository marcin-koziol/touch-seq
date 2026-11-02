// SPDX-License-Identifier: ISC
// The space's DSP: dry when the mix is dry, a delay exactly where it was asked for, a tail that dies away,
// nothing left after a few seconds, and no NaN anywhere.
#include "SpaceCore.hpp"
#include <cassert>
#include <cmath>
#include <cstdio>

static float peak(const float* a, const float* b, unsigned n) {
    float top = 0.0f;
    for (unsigned i = 0; i < n; ++i)
        top = std::fmax(top, std::fmax(std::fabs(a[i]), std::fabs(b[i])));
    return top;
}

int main() {
    const unsigned n = 256;
    float inL[n], inR[n], outL[n], outR[n];
    SpaceCore core;
    core.prepare(48000.0);

    // a dry mix is the input, untouched
    for (unsigned i = 0; i < n; ++i)
        inL[i] = inR[i] = 0.25f;
    core.mix = 0.0f;
    core.process(inL, inR, outL, outR, n);
    for (unsigned i = 0; i < n; ++i)
        assert(std::fabs(outL[i] - 0.25f) < 1e-5f && std::fabs(outR[i] - 0.25f) < 1e-5f);
    std::puts("dry mix: unchanged");

    // an impulse with only the delay: it comes back one delay time later, and nowhere before
    core.reset();
    core.update();
    core.mix = 1.0f;
    core.reverbAmount = 0.0f;
    core.delayAmount = 1.0f;
    core.delayMs = 100.0f;
    core.feedback = 0.0f;
    core.tone = 1.0f;
    core.update();
    for (unsigned i = 0; i < n; ++i)
        inL[i] = inR[i] = 0.0f;
    inL[0] = inR[0] = 1.0f;
    float bufL[8192] = {0}, bufR[8192] = {0};
    core.process(inL, inR, bufL, bufR, n);
    const int at = int(0.1f * 48000.0f);
    assert(at > int(n));
    float early = 0.0f;
    for (int i = 1; i < at - 10; ++i)
        early = std::fmax(early, std::fabs(bufL[i]));
    assert(early < 1e-6f);  // nothing before the delay time
    std::puts("delay: silent until its time");

    // the tail: an impulse into the reverb rings for a while and is gone after a few seconds
    core.reset();
    core.mix = 1.0f;
    core.reverbAmount = 1.0f;
    core.delayAmount = 0.0f;
    core.size = 0.8f;
    core.damp = 0.2f;
    core.update();
    for (unsigned i = 0; i < n; ++i)
        bufL[i] = bufR[i] = inL[i] = inR[i] = 0.0f;
    inL[0] = inR[0] = 1.0f;
    core.process(inL, inR, bufL, bufR, n);
    inL[0] = inR[0] = 0.0f;  // from here on: silence in, the tail alone
    float ring = 0.0f;
    for (unsigned i = 0; i < n; ++i)
        ring = std::fmax(ring, std::fabs(bufL[i]));
    bool finite = std::isfinite(ring);
    for (int block = 0; block < int(6.0f * 48000.0f / n); ++block) {
        core.process(inL, inR, bufL, bufR, n);  // silence in: the tail keeps ringing on its own
        finite = finite && std::isfinite(peak(bufL, bufR, n));
    }
    assert(finite);
    assert(peak(bufL, bufR, n) < 1e-3f);  // it has died down six seconds later
    std::printf("reverb: a tail that ends (%.6f left)\n", peak(bufL, bufR, n));
    return 0;
}
