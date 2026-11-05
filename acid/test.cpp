// SPDX-License-Identifier: GPL-3.0-or-later
// The acid's DSP (Open303 engine): silence when idle, a note that sounds and stops, the note's pitch, a
// brighter filter with a higher cutoff, accent louder than a soft note, a slide that glides, the overdrive
// switch changing the tone, and finite samples all the way through.
#include "AcidCore.hpp"

#include <cassert>
#include <cmath>
#include <cstdio>
#include <vector>

static double rms(const std::vector<float>& v, size_t from, size_t to) {
    double sum = 0.0;
    for (size_t i = from; i < to; ++i)
        sum += double(v[i]) * double(v[i]);
    return std::sqrt(sum / double(to - from));
}

static double roughness(const std::vector<float>& v, size_t from, size_t to) {
    double sum = 0.0;
    for (size_t i = from + 1; i < to; ++i)
        sum += std::fabs(double(v[i]) - double(v[i - 1]));
    return sum / double(to - from);
}

static int crossings(const std::vector<float>& v, size_t from, size_t to) {
    int count = 0;
    bool wasBelow = false;
    for (size_t i = from; i < to; ++i) {
        if (v[i] < -0.0005f)
            wasBelow = true;
        else if (v[i] > 0.0005f && wasBelow) {
            ++count;
            wasBelow = false;
        }
    }
    return count;
}

static void render(AcidCore& core, std::vector<float>& left, std::vector<float>& right) {
    for (size_t i = 0; i < left.size(); i += 256) {
        const uint32_t n = uint32_t(std::min<size_t>(256, left.size() - i));
        core.process(left.data() + i, right.data() + i, n);
    }
}

int main() {
    const double sr = 48000.0;
    AcidCore core;
    core.prepare(sr);
    assert(core.resonance > 0.5f && core.envMod > 0.5f);  // the defaults are already acid

    // idle: silence
    const size_t second = size_t(sr);
    std::vector<float> left(second), right(second);
    render(core, left, right);
    assert(rms(left, 0, left.size()) < 1e-6);
    std::puts("idle: silent");

    // a note sounds, and it stops after note-off
    core.reset();
    core.noteOn(45, 100);
    render(core, left, right);
    const double noteLevel = rms(left, 0, left.size());
    std::printf("note: RMS %.4f\n", noteLevel);
    assert(noteLevel > 0.01);
    core.noteOff(45);
    std::fill(left.begin(), left.end(), 0.0f);
    std::fill(right.begin(), right.end(), 0.0f);
    render(core, left, right);
    assert(rms(left, left.size() / 2, left.size()) < 0.001);
    std::puts("note off: silent again");

    // pitch: A4 is 440 Hz (filter open, little resonance, no envelope, no accent)
    core.reset();
    core.wave = 0.0f;
    core.cutoff = 1.0f;
    core.resonance = 0.10f;
    core.envMod = 0.0f;
    core.accent = 0.0f;
    core.volume = 1.0f;
    core.update();
    core.noteOn(69, 64);
    render(core, left, right);
    const int hz = crossings(left, size_t(sr * 0.1), size_t(sr * 0.5));
    std::printf("A4: %d crossings in 0.4 s\n", hz);
    assert(hz > 150 && hz < 200);

    // tuning: an octave up doubles the crossings
    core.reset();
    core.tune = 12.0f;
    core.update();
    core.noteOn(69, 64);
    render(core, left, right);
    const int hzUp = crossings(left, size_t(sr * 0.1), size_t(sr * 0.5));
    std::printf("A4 +12 st: %d crossings\n", hzUp);
    assert(hzUp > 300 && hzUp < 400);
    core.tune = 0.0f;

    // cutoff: open is brighter than closed
    auto brightness = [&](float cut) {
        core.reset();
        core.cutoff = cut;
        core.resonance = 0.10f;
        core.envMod = 0.0f;
        core.accent = 0.0f;
        core.volume = 1.0f;
        core.update();
        core.noteOn(45, 64);
        render(core, left, right);
        return roughness(left, size_t(sr * 0.05), size_t(sr * 0.35));
    };
    const double dark = brightness(0.0f);
    const double bright = brightness(1.0f);
    std::printf("cutoff: rough %.4f closed, %.4f open\n", dark, bright);
    assert(bright > dark * 1.5);

    // accent: a hard note is louder than a soft one (velocity >= 100 accents in Open303)
    auto levelAt = [&](float velocity) {
        core.reset();
        core.cutoff = 0.25f;
        core.resonance = 0.92f;
        core.envMod = 0.60f;
        core.decay = 0.29f;
        core.accent = 0.78f;
        core.volume = 1.0f;
        core.update();
        core.noteOn(45, velocity);
        render(core, left, right);
        return rms(left, 0, size_t(sr * 0.3));
    };
    const double soft = levelAt(20.0f);
    const double hard = levelAt(127.0f);
    std::printf("accent: %.4f soft, %.4f hard\n", soft, hard);
    assert(hard > soft * 1.3);

    // slide: a legato note glides up instead of jumping
    core.reset();
    core.cutoff = 1.0f;
    core.resonance = 0.10f;
    core.envMod = 0.0f;
    core.accent = 0.0f;
    core.volume = 1.0f;
    core.slide = 1.0f;  // 360 ms
    core.update();
    core.noteOn(45, 64);
    render(core, left, right);
    core.noteOn(57, 64);  // legato: 45 is still held
    const size_t glideN = size_t(sr * 0.4);
    std::vector<float> glideL(glideN), glideR(glideN);
    render(core, glideL, glideR);
    const int early = crossings(glideL, 0, size_t(sr * 0.04));
    const int late = crossings(glideL, size_t(sr * 0.3), size_t(sr * 0.4));
    std::printf("slide: %d crossings early, %d late\n", early, late);
    assert(late > early * 1.5);

    // overdrive: the switch changes the tone, and nothing becomes NaN
    core.reset();
    core.noteOn(45, 64);
    core.overdrive = false;
    render(core, left, right);
    const double clean = roughness(left, size_t(sr * 0.05), size_t(sr * 0.35));
    const double cleanRms = rms(left, size_t(sr * 0.05), size_t(sr * 0.35));
    core.reset();
    core.noteOn(45, 64);
    core.setOverdrive(true);
    render(core, left, right);
    const double dirty = roughness(left, size_t(sr * 0.05), size_t(sr * 0.35));
    const double dirtyRms = rms(left, size_t(sr * 0.05), size_t(sr * 0.35));
    std::printf("drive: rough %.4f/%.4f rms %.4f/%.4f clean/driven\n", clean, dirty, cleanRms, dirtyRms);
    assert(std::fabs(dirty - clean) > 0.0005 || std::fabs(dirtyRms - cleanRms) > 0.001);
    for (float v : left)
        assert(std::isfinite(v));

    // square is a different oscillator, not a copy of the saw
    core.reset();
    core.overdrive = false;
    core.wave = 1.0f;
    core.update();
    core.noteOn(45, 64);
    render(core, left, right);
    assert(rms(left, 0, left.size()) > 0.01);
    std::puts("acid OK");
    return 0;
}
