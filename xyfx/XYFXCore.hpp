// SPDX-License-Identifier: ISC
// Touch-Seq XYFX: beat repeat (X) + filter (Y), active while engaged (a finger on the XY pad).
// X and Y are pad coordinates; the centre is the mildest spot and the effect grows towards the edges.
//   X, 8 columns: the right half repeats straight divisions 1/2 1/4 1/8 1/16 (centre outwards); the left
//      half the same ladder as triplets (mode 0: 1/2T 1/4T 1/8T 1/16T) or played backwards (mode 1).
//   Y: open on the centre line; below it a low-pass closes (80 Hz at the bottom edge), above it a
//      high-pass opens (6 kHz at the top edge), like a DJ mixer's filter knob.
// Plain DSP, no plugin framework, so it can be tested on its own.
#pragma once

#include <algorithm>
#include <cmath>
#include <vector>

class XYFXCore {
public:
    // repeat lengths from the centre outwards, in beats: 1/2, 1/4, 1/8, 1/16 note
    static constexpr int kDivisions = 4;
    static constexpr double kBeats[kDivisions] = {2.0, 1.0, 0.5, 0.25};
    enum Mode { kTriplets = 0, kReverse = 1 };

    float engage = 0.0f;  // > 0.5: on
    float x = 0.5f;       // 0..1 across the pad
    float y = 0.5f;       // 0..1 up the pad
    float bpm = 120.0f;
    float mode = kTriplets;  // what the left half does

    void prepare(double sampleRate)
    {
        sr = sampleRate;
        // the longest repeat: a half note at 30 BPM = 4 s
        cap = static_cast<size_t>(sr * 4.5);
        for (auto& b : buf)
            b.assign(cap, 0.0f);
        t = 0;
        mix = 0.0f;
        on = false;
        highPass = y > 0.5f;
        cutoff = cutoffFor(y);
        std::fill(std::begin(ic1), std::end(ic1), 0.0f);
        std::fill(std::begin(ic2), std::end(ic2), 0.0f);
    }

    // column 0..7 -> 0 (1/2, next to the centre) .. 3 (1/16, the outer columns)
    static int division(float x)
    {
        const int col = std::clamp(static_cast<int>(x * 8), 0, 7);
        return col < 4 ? 3 - col : col - 4;
    }
    static bool leftHalf(float x) { return x < 0.5f; }

    // how far from the centre line the filter is, 0..1
    static float filterAmount(float y) { return std::min(1.0f, std::fabs(y - 0.5f) * 2.0f); }
    // low-pass 20 kHz -> 80 Hz below the centre line; high-pass 20 Hz -> 6 kHz above it
    static float cutoffFor(float y)
    {
        const float a = filterAmount(y);
        return y > 0.5f ? 20.0f * std::pow(300.0f, a) : 20000.0f / std::pow(250.0f, a);
    }

    bool triplet() const { return leftHalf(x) && static_cast<int>(mode + 0.5f) == kTriplets; }
    bool reversed() const { return leftHalf(x) && static_cast<int>(mode + 0.5f) == kReverse; }

    size_t repeatLength() const
    {
        const double beats = kBeats[division(x)] * (triplet() ? 2.0 / 3.0 : 1.0);
        const double seconds = beats * 60.0 / std::max(20.0f, bpm);
        return std::clamp(static_cast<size_t>(seconds * sr), size_t(64), cap);
    }

    void process(const float* inL, const float* inR, float* outL, float* outR, unsigned frames)
    {
        const bool engaged = engage > 0.5f;
        if (engaged && !on)
            t = 0;  // a new touch: capture starts now
        on = engaged;
        if (!on && mix <= 0.0f) {  // idle: dry
            std::copy(inL, inL + frames, outL);
            std::copy(inR, inR + frames, outR);
            return;
        }

        const size_t len = repeatLength();
        const bool backwards = reversed();
        const size_t fade = std::min<size_t>(static_cast<size_t>(sr * 0.002), len / 4);  // 2 ms at repeat edges
        const float mixStep = 1.0f / static_cast<float>(sr * 0.01);                      // 10 ms in / out

        // one cutoff per block, glided so sweeps don't zipper; crossing the centre line switches the
        // filter type where both are wide open, so it snaps instead of sweeping across the range
        if ((y > 0.5f) != highPass) {
            highPass = y > 0.5f;
            cutoff = highPass ? 20.0f : 20000.0f;
        }
        cutoff += (cutoffFor(y) - cutoff) * std::min(1.0f, frames / static_cast<float>(sr * 0.02));
        const float g = std::tan(static_cast<float>(M_PI) * std::min(cutoff, static_cast<float>(sr * 0.45)) /
                                 static_cast<float>(sr));
        const float k = 1.41421356f;  // Q = 0.707
        const float a1 = 1.0f / (1.0f + g * (g + k)), a2 = g * a1, a3 = g * a2;

        const float* in[2] = {inL, inR};
        float* out[2] = {outL, outR};
        for (unsigned i = 0; i < frames; ++i) {
            if (on && t < cap) {
                buf[0][t] = inL[i];
                buf[1][t] = inR[i];
            }
            const size_t pos = t % len;
            // the first pass is the live input being captured; repeats (backwards ones read the slice from
            // its end) fade in/out at their edges
            const size_t idx = backwards && t >= len ? len - 1 - pos : pos;
            float env = 1.0f;
            if (t >= len && fade > 0)
                env = std::min(1.0f, std::min(pos, len - 1 - pos) / static_cast<float>(fade));
            for (int c = 0; c < 2; ++c) {
                const float v0 = buf[c][idx] * env;  // idx < len <= cap: always captured by now
                const float v3 = v0 - ic2[c];
                const float v1 = a1 * ic1[c] + a2 * v3;
                const float v2 = ic2[c] + a2 * ic1[c] + a3 * v3;
                ic1[c] = 2.0f * v1 - ic1[c];
                ic2[c] = 2.0f * v2 - ic2[c];
                const float wet = highPass ? v0 - k * v1 - v2 : v2;
                out[c][i] = in[c][i] * (1.0f - mix) + wet * mix;
            }
            mix = on ? std::min(1.0f, mix + mixStep) : std::max(0.0f, mix - mixStep);
            if (on)
                ++t;
        }
    }

private:
    double sr = 48000.0;
    size_t cap = 0, t = 0;
    std::vector<float> buf[2];
    bool on = false, highPass = false;
    float mix = 0.0f, cutoff = 20000.0f;
    float ic1[2] = {}, ic2[2] = {};
};
