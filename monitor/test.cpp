// SPDX-License-Identifier: ISC
// The monitor's DSP: transparent at 0 dB and centre, balance panning, gain in dB.
#include "MonitorCore.hpp"
#include <cassert>
#include <cmath>
#include <cstdio>

int main() {
    const unsigned n = 64;
    float inL[n], inR[n], outL[n], outR[n];
    for (unsigned i = 0; i < n; ++i)
        inL[i] = inR[i] = 0.5f;

    MonitorCore core;
    core.prepare(48000.0);
    core.update();
    core.reset();
    core.process(inL, inR, outL, outR, n);
    assert(std::fabs(outL[n - 1] - 0.5f) < 1e-6f and std::fabs(outR[n - 1] - 0.5f) < 1e-6f);  // transparent
    std::puts("0 dB, centre: unchanged");

    core.pan = -1.0f;  // hard left: the right side goes away, the left stays where it was
    core.update();
    core.reset();
    core.process(inL, inR, outL, outR, n);
    assert(std::fabs(outL[n - 1] - 0.5f) < 1e-6f and std::fabs(outR[n - 1]) < 1e-6f);
    std::puts("pan left: right silent");

    core.pan = 1.0f;
    core.update();
    core.reset();
    core.process(inL, inR, outL, outR, n);
    assert(std::fabs(outL[n - 1]) < 1e-6f and std::fabs(outR[n - 1] - 0.5f) < 1e-6f);
    std::puts("pan right: left silent");

    core.pan = 0.0f;
    core.gainDb = -6.0f;  // half the amplitude
    core.update();
    core.reset();
    core.process(inL, inR, outL, outR, n);
    const float half = 0.5f * std::pow(10.0f, -6.0f / 20.0f);  // -6 dB is 0.5 amplitude, near enough
    assert(std::fabs(outL[n - 1] - half) < 1e-6f);
    std::puts("-6 dB: half");

    core.gainDb = -60.0f;  // silence
    core.update();
    core.reset();
    core.process(inL, inR, outL, outR, n);
    assert(std::fabs(outL[n - 1]) < 1e-6f);
    std::puts("-60 dB: silent");

    // a parameter turned mid-stream fades, it does not jump
    core.gainDb = 0.0f;
    core.update();
    core.reset();
    core.gainDb = -60.0f;
    core.update();
    core.process(inL, inR, outL, outR, 1);
    assert(outL[0] < 0.5f and outL[0] > 0.4f);  // one sample of smoothing, not a step to zero
    std::puts("a turn is smoothed");

    std::puts("monitor DSP OK");
    return 0;
}
