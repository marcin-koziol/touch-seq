// SPDX-License-Identifier: ISC
// The DSP of Touch-Seq Click: the metronome of the app, rendered where the music is. Touch-Seq's engine
// sends one note per beat (one number for the first beat of the bar, another for the rest), so the click is
// played by the audio graph itself, in the same cycle as the instruments: there is nothing to synchronise,
// and nothing can drift away from the notes.
#ifndef CLICK_CORE_HPP_INCLUDED
#define CLICK_CORE_HPP_INCLUDED

#include <cmath>

class ClickCore {
public:
    static constexpr int kAccentNote = 76;  // the first beat of the bar
    static constexpr int kBeatNote = 77;    // every other beat

    float level = 0.7f;  // 0 .. 1, the click's own level

    void prepare(double sampleRate) {
        rate = sampleRate > 1000.0 ? float(sampleRate) : 48000.0f;
        tail = int(0.05f * rate);  // a click is 50 ms of decaying sine: beats 100 ms apart never overlap
        reset();
    }

    void reset() { age = tail; }  // silent

    void noteOn(int note, int velocity) {
        const bool accent = note == kAccentNote;
        amplitude = level * (accent ? 0.9f : 0.5f) * std::fmin(1.0f, float(velocity) / 100.0f);
        frequency = accent ? 1660.0f : 1180.0f;  // the first beat of the bar is the higher one
        age = 0;
    }

    bool ringing() const { return age < tail; }

    // One sample of the click (0 when nothing is ringing).
    float tick() {
        if (age >= tail)
            return 0.0f;
        const float seconds = float(age) / rate;
        const float envelope = std::exp(-seconds / 0.006f);
        const float sample = amplitude * envelope * std::sin(6.2831853f * frequency * seconds);
        ++age;
        return sample;
    }

private:
    float rate = 48000.0f, amplitude = 0.0f, frequency = 1180.0f;
    int tail = 2400, age = 2400;
};

#endif // CLICK_CORE_HPP_INCLUDED
