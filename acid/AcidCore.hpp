// SPDX-License-Identifier: GPL-3.0-or-later
// Touch-Seq Acid's engine: a thin adapter around Robin Schmidt's Open303 (MIT), the same engine jc303 uses.
// The public fields are normalized (0..1) for Touch-Seq's knobs; the adapter maps them to Open303's real
// units. The overdrive is a switch after the engine: a fixed soft-clip stage, not a continuous drive knob.
//
// Open303 is Copyright (c) 2009 Robin Schmidt (www.rs-met.com), MIT licensed: see open303/LICENSE.
#ifndef ACID_CORE_HPP_INCLUDED
#define ACID_CORE_HPP_INCLUDED

#include <algorithm>
#include <cmath>
#include <cstdint>

#include "rosic_Open303.h"
#include "Overdrive.hpp"

class AcidCore {
public:
    // 0..1 unless marked otherwise; the plugin wrapper owns the parameter ranges.
    float wave = 0.0f;       // 0 saw, 1 square
    float tune = 0.0f;       // semitones, -12..12
    float cutoff = 0.25f;
    float resonance = 0.92f;
    float envMod = 0.60f;
    float decay = 0.29f;
    float accent = 0.78f;
    bool overdrive = false;  // the switch, not an amount
    float slide = 0.16f;
    float volume = 0.75f;

    void prepare(double sampleRate) {
        rate = sampleRate > 1000.0 ? sampleRate : 48000.0;
        engine.setSampleRate(rate);
        engine.setFilterType(rosic::FILTER_TEEBEE);
        engine.setFilterDrive(0.0);
        engine.setPassbandCompensation(0.0);
        engine.setLfoOn(false);
        engine.setTanhShaperDrive(36.9);  // the classic 303 square
        engine.setNormalAttack(3.0);
        engine.setAccentAttack(3.0);
        engine.setAccentDecay(200.0);
        engine.setAmpDecay(1230.0);
        engine.setAmpRelease(0.5);
        engine.setFeedbackHighpass(150.0);
        engine.setPitchBend(0.0);
        od.prepare(rate);
        update();
        reset();
    }

    void update() {
        engine.setWaveform(wave > 0.5f ? 1.0 : 0.0);
        engine.setTuning(440.0 * std::pow(2.0, tune / 12.0));
        engine.setCutoff(314.0 * std::pow(2394.0 / 314.0, clamp01(cutoff)));
        engine.setResonance(clamp01(resonance) * 100.0);
        engine.setEnvMod(clamp01(envMod) * 100.0);
        engine.setDecay(200.0 * std::pow(2000.0 / 200.0, clamp01(decay)));
        engine.setAccent(clamp01(accent) * 100.0);
        engine.setVolume(-60.0 + clamp01(volume) * 60.0);
        engine.setSlideTime(2.0 + clamp01(slide) * 358.0);
    }

    void reset() {
        engine.allNotesOff();
        engine.setPitchBend(0.0);
        od.reset();
    }

    void setOverdrive(bool on) {
        if (on && !overdrive)
            od.reset();  // the LSTM starts clean when the switch goes on
        overdrive = on;
    }

    void noteOn(int note, float velocity) {
        engine.noteOn(std::max(0, std::min(127, note)), std::max(0, std::min(127, int(velocity + 0.5f))), 0.0);
    }

    void noteOff(int note) {
        engine.noteOn(std::max(0, std::min(127, note)), 0, 0.0);
    }

    void allNotesOff() {
        engine.allNotesOff();
    }

    void pitchBend(float value) {
        engine.setPitchBend(std::max(-1.0f, std::min(1.0f, value)) * 2.0);
    }

    void process(float* left, float* right, uint32_t frames) {
        for (uint32_t i = 0; i < frames; ++i) {
            float sample = float(engine.getSample());
            if (overdrive)
                sample = od.process(sample);
            if (!std::isfinite(sample))
                sample = 0.0f;
            left[i] = right[i] = sample;
        }
    }

private:
    static float clamp01(float v) {
        return std::max(0.0f, std::min(1.0f, v));
    }

    double rate = 48000.0;
    NeuralOverdrive od;
    rosic::Open303 engine;
};

#endif // ACID_CORE_HPP_INCLUDED
