// SPDX-License-Identifier: GPL-3.0-or-later
// The sampler's engine: 16 pads, each a slice of a WAV file, played from MIDI notes 36..51. The pad map is a
// small text manifest written by Touch-Seq's sampler editor in <project>.plugins/sampler/; the plugin reads it
// from TOUCHSEQ_SAMPLER_DIR + sampler/sampler-<slot>.txt, so samples travel with the project and nothing has
// to go through Carla's state. Mono samples, linear interpolation, one-shot or looped, 16 voices. STRETCH
// shifts the pitch without changing the slice's length through Rubber Band (GPL-2.0-or-later), vendored in
// rubberband/ and linked statically.
#ifndef SAMPLER_CORE_HPP_INCLUDED
#define SAMPLER_CORE_HPP_INCLUDED

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <rubberband/RubberBandLiveShifter.h>

#include "dr_wav.h"

struct Envelope {
    float attack = 0.0f, decay = 0.0f, sustain = 1.0f, release = 0.0f;  // fractions of the slice

    // The value at `t` (0..1 through the slice). A loop never runs the release segment: it holds sustain.
    float value(float t, bool withRelease) const {
        if (t <= 0.0f)
            return attack > 0.0f ? 0.0f : 1.0f;
        if (attack > 0.0f && t < attack)
            return t / attack;
        const float d = attack + decay;
        if (decay > 0.0f && t < d)
            return 1.0f - (1.0f - sustain) * (t - attack) / decay;
        if (withRelease && release > 0.0f) {
            const float r = 1.0f - release;
            if (t >= r)
                return sustain * std::max(0.0f, 1.0f - (t - r) / release);
        }
        return sustain;
    }
};

struct SamplerPad {
    std::shared_ptr<const std::vector<float>> data;  // mono, the whole file (shared by light map updates)
    std::string file;  // its name, so a map update can reuse the samples it already has
    uint32_t fileRate = 48000;
    uint32_t start = 0;
    uint32_t end = 0;
    int root = 36;
    float gain = 1.0f;
    float pan = 0.0f;
    float pitch = 0.0f;
    float fine = 0.0f;  // cents, added to PITCH: -100..+100
    int group = 0;  // playback group: only one pad of a non-zero group sounds at a time
    bool loop = false;
    bool reverse = false;
    bool gate = false;  // play only while the note is held; one-shot plays to the end
    bool stretch = false;  // keep the slice's length when PITCH changes (granular pitch shift)
    Envelope amp, filter;  // amp and filter envelopes, both over the slice
    float cutoff = 20000.0f;  // low-pass, Hz
    float reso = 0.0f;        // 0..1
    float envAmount = 0.0f;   // filter envelope depth, octaves
    int lfoWave = 0;          // 0 sine, 1 triangle, 2 square, 3 saw
    float lfoRate = 2.0f;     // Hz
    float lfoDepth = 0.0f;    // 0..1
    int lfoTarget = 0;        // 0 amp, 1 filter, 2 pitch
    bool lfoSync = false;     // follow the tempo instead of lfoRate
    int lfoDiv = 2;           // 0 1/1 .. 6 1/16T (see lfoRateHz)

    bool loaded() const { return data && end > start && end <= data->size(); }
};

struct SamplerBank {
    std::array<SamplerPad, 16> pads;
    bool mono = false;
};

class SamplerCore {
public:
    static constexpr int kPads = 16;
    static constexpr int kNoteBase = 36;

    void prepare(double sampleRate);
    void setSlot(int slot);
    void setTempo(float bpm) { tempo = std::max(20.0f, std::min(300.0f, bpm)); }
    void reload() { load(); }
    void applyMap();  // a light reload: the pad map changed, the samples are reused

    void noteOn(int note, int velocity);
    void noteOff(int note);
    void allNotesOff();
    int activeVoices() const;
    // The newest active voice: its position within the pad's slice (0..1) and the pad, or pad -1 when silent.
    void playhead(float& position, int& pad) const;

    void process(float* left, float* right, uint32_t frames);

private:
    struct Voice {
        std::shared_ptr<const SamplerBank> bank;
        int pad = -1;
        int note = 36;              // the note that started it, to recompute the step on a map update
        float velocityGain = 1.0f;
        double pos = 0.0;
        double step = 1.0;           // the playback step, pitch included (without stretch)
        float gain = 1.0f;
        float panL = 0.707107f;
        float panR = 0.707107f;
        float release = 1.0f;
        bool loop = false;
        bool gate = false;
        bool releasing = false;
        bool active = false;
        uint64_t age = 0;
        bool stretch = false;        // Rubber Band pitch shift: PITCH does not change the slice's length
        double baseStep = 1.0;       // the timeline (note transposition), without the pad's PITCH
        double clock = 0.0;          // output frames since note-on, for the envelope
        double lfoPhase = 0.0;       // 0..1, reset on note-on
        float ic1eq = 0.0f, ic2eq = 0.0f;  // the voice's low-pass state
        // the stretch voice's shifter: the slice feeds it at the timeline speed, it returns the same number
        // of frames at the pad's pitch, delayed by its own latency
        std::unique_ptr<RubberBand::RubberBandLiveShifter> shifter;
        std::vector<float> shiftIn, shiftOut;
        size_t shiftBlock = 0;       // frames per shift() call, fixed by the shifter
        size_t shiftRead = 0;        // the next frame to take from shiftOut
        double shiftDelay = 0.0;     // the shifter's output latency, in frames
        bool shiftDrain = false;     // the slice ended: feed zeros while the delayed tail comes out
        int drainLeft = 0;           // frames of that tail still to play
    };

    void load();
    void adopt(Voice& voice, const std::shared_ptr<const SamplerBank>& next);  // point a playing voice at a new map
    void setVoiceStep(Voice& voice, const SamplerPad& pad);
    void resetShift(Voice& voice, const SamplerPad& pad);  // a fresh shifter for a new note
    bool nextInput(Voice& voice, const SamplerPad& pad, float& value);  // one frame at the timeline speed

    double rate = 48000.0;
    float tempo = 120.0f;  // the project's BPM, for a tempo-synced LFO
    int slot = 0;
    std::shared_ptr<const SamplerBank> bank;
    std::shared_ptr<const SamplerBank> pending;  // a light map update, adopted by the audio thread
    std::array<Voice, 16> voices;
    uint64_t nextAge = 0;
    float releaseStep = 0.01f;  // one sample's share of the 5 ms note-off fade
};

#endif // SAMPLER_CORE_HPP_INCLUDED
