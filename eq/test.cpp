// SPDX-License-Identifier: ISC
// The EQ's DSP: a flat curve by default, a bell where it is asked for, cuts that cut, and an analyser that
// shows a tone where the tone is.
#include "EQCore.hpp"
#include <cassert>
#include <cmath>
#include <cstdio>

static float rms(float (*fn)(unsigned), unsigned n) {
    double sum = 0.0;
    for (unsigned i = 0; i < n; ++i) {
        const float v = fn(i);
        sum += double(v) * v;
    }
    return float(std::sqrt(sum / n));
}

int main() {
    const unsigned n = 4800;  // 0.1 s at 48k
    float l[n], r[n];

    EQCore core;
    core.prepare(48000.0);
    core.settle();
    for (unsigned i = 0; i < n; ++i) l[i] = r[i] = 0.5f;
    core.process(l, r, n);
    assert(std::fabs(l[n - 1] - 0.5f) < 1e-3f and std::fabs(r[n - 1] - 0.5f) < 1e-3f);  // flat by default
    std::puts("flat: a 0 dB curve passes the signal");

    // the default curve is flat everywhere, in dB
    for (float f : {40.0f, 200.0f, 1000.0f, 5000.0f, 15000.0f})
        assert(std::fabs(core.magnitudeDb(f)) < 0.01f);
    std::puts("flat: the response is 0 dB");

    // a bell of +12 dB at 1 kHz lifts that octave and leaves 40 Hz alone
    core.bands[3] = Band{1000.0f, 12.0f, 1.4f, int(BandType::Bell)};
    core.settle();
    assert(std::fabs(core.magnitudeDb(1000.0f) - 12.0f) < 0.05f);
    assert(std::fabs(core.magnitudeDb(40.0f)) < 0.6f);
    std::puts("bell: +12 dB at its centre");

    // a low cut takes the bass away and keeps the top
    core.resetDefaults();
    core.bands[0] = Band{200.0f, 0.0f, 0.7f, int(BandType::LowCut)};
    core.settle();
    assert(core.magnitudeDb(30.0f) < -20.0f and std::fabs(core.magnitudeDb(5000.0f)) < 0.1f);
    std::puts("low cut: the bass goes, the top stays");

    // a high cut is the other way round
    core.resetDefaults();
    core.bands[7] = Band{4000.0f, 0.0f, 0.7f, int(BandType::HighCut)};
    core.settle();
    assert(core.magnitudeDb(15000.0f) < -20.0f and std::fabs(core.magnitudeDb(200.0f)) < 0.1f);
    std::puts("high cut: the top goes, the bass stays");

    // the shelves lean the right way
    core.resetDefaults();
    core.bands[0] = Band{300.0f, 9.0f, 0.7f, int(BandType::LowShelf)};
    core.bands[7] = Band{3000.0f, -9.0f, 0.7f, int(BandType::HighShelf)};
    core.settle();
    assert(core.magnitudeDb(30.0f) > 7.0f and core.magnitudeDb(15000.0f) < -7.0f);
    std::puts("shelves: low shelf lifts the bass, high shelf drops the top");

    // the smoothing: a jump in gain does not jump in the first sample
    core.resetDefaults();
    core.settle();
    core.bands[2].gain = 24.0f;
    core.touch();
    for (unsigned i = 0; i < 64; ++i) l[i] = r[i] = 0.5f;
    core.process(l, r, 64);
    assert(l[0] > 0.4f and l[0] < 0.6f);  // the curve moved a little, not 16x
    std::puts("a turn is smoothed");

    // the analyser: 1 kHz at 0.5 should light the band around 1 kHz, not the ones at 40 Hz or 15 kHz
    core.resetDefaults();
    core.settle();
    double phase = 0.0;
    for (int block = 0; block < 40; ++block) {
        for (unsigned i = 0; i < 512; ++i) {
            l[i] = r[i] = 0.5f * float(std::sin(phase));
            phase += 2.0 * 3.14159265358979323846 * 1000.0 / 48000.0;
        }
        core.process(l, r, 512);
    }
    auto bandOf = [](double hz) {
        return int(std::log(hz / 20.0) / std::log(20000.0 / 20.0) * EQCore::kSpectrumCount);
    };
    const float atTone = core.spectrum[bandOf(1000.0)], atBass = core.spectrum[bandOf(40.0)],
                atTop = core.spectrum[bandOf(15000.0)];
    assert(atTone > 0.6f and atBass < atTone - 0.3f and atTop < atTone - 0.3f);
    std::printf("analyser: tone band %.2f, bass %.2f, top %.2f\n", atTone, atBass, atTop);

    std::puts("EQ DSP OK");
    return 0;
}
