// SPDX-License-Identifier: ISC
// The click's DSP: silent until a beat arrives, a decaying tick that is gone before the next beat, the
// first beat of the bar higher and louder, the level a parameter.
#include "ClickCore.hpp"
#include <cassert>
#include <cmath>
#include <cstdio>

static float peak(ClickCore& core, int samples) {
    float top = 0.0f;
    for (int i = 0; i < samples; ++i)
        top = std::fmax(top, std::fabs(core.tick()));
    return top;
}

int main() {
    ClickCore core;
    core.prepare(48000.0);
    core.reset();
    assert(!core.ringing() and peak(core, 4800) == 0.0f);  // a fresh plugin is silent
    std::puts("silent until a beat");

    core.noteOn(ClickCore::kBeatNote, 100);
    assert(core.ringing());
    const float first = peak(core, 64);
    assert(first > 0.1f);  // it starts at once, not with a fade-in
    assert(peak(core, int(0.05f * 48000.0f)) > 0.001f);  // ... and decays over its tail
    assert(!core.ringing());  // gone well before the next beat (100 ms at 120 bpm)
    std::puts("a beat: a tick that ends inside 50 ms");

    core.reset();
    core.noteOn(ClickCore::kAccentNote, 127);
    const float accent = peak(core, 512);
    core.reset();
    core.noteOn(ClickCore::kBeatNote, 127);
    const float plain = peak(core, 512);
    assert(accent > plain * 1.4f);  // the first beat of the bar stands out
    std::printf("accent %.3f vs %.3f\n", accent, plain);

    core.reset();
    core.noteOn(ClickCore::kAccentNote, 127);
    core.level = 0.0f;
    core.reset();
    core.noteOn(ClickCore::kAccentNote, 127);
    assert(peak(core, 512) == 0.0f);  // level 0 is silence
    std::puts("level 0: the click goes away");
    return 0;
}
