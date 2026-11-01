// SPDX-License-Identifier: ISC
// The DSP of Touch-Seq Monitor: a transparent gain/pan node whose only job is to give an audio track's input
// a place in the graph (a node Carla can meter, and a chain whose fader means something).
#ifndef MONITOR_CORE_HPP_INCLUDED
#define MONITOR_CORE_HPP_INCLUDED

#include <cmath>

class MonitorCore {
public:
    float gainDb = 0.0f;   // -60 .. +12, 0 is transparent
    float pan = 0.0f;      // -1 left, 0 centre, +1 right; balance, so the centre is unity on both sides
    float left = 1.0f, right = 1.0f;  // smoothed gains actually applied

    void prepare(double sampleRate) { smoothing = 1.0f - std::exp(-1.0f / (0.02f * float(sampleRate))); }

    void update() {
        const float gain = gainDb <= -60.0f ? 0.0f : std::pow(10.0f, gainDb / 20.0f);
        targetLeft = gain * std::fmin(1.0f, 1.0f - pan);
        targetRight = gain * std::fmin(1.0f, 1.0f + pan);
    }

    void reset() { left = targetLeft; right = targetRight; }  // a fresh start is not a fade-in

    void process(const float* inL, const float* inR, float* outL, float* outR, unsigned frames) {
        for (unsigned i = 0; i < frames; ++i) {
            left += (targetLeft - left) * smoothing;
            right += (targetRight - right) * smoothing;
            outL[i] = inL[i] * left;
            outR[i] = inR[i] * right;
        }
    }

private:
    float targetLeft = 1.0f, targetRight = 1.0f, smoothing = 0.01f;
};

#endif // MONITOR_CORE_HPP_INCLUDED
