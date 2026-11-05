// SPDX-License-Identifier: ISC
// The sampler's engine: 16 pads, each a slice of a WAV file, played from MIDI notes 36..51. The pad map is a
// small text manifest written by Touch-Seq's sampler editor in <project>.plugins/sampler/; the plugin reads it
// from TOUCHSEQ_SAMPLER_DIR + sampler/sampler-<slot>.txt, so samples travel with the project and nothing has
// to go through Carla's state. Mono samples, linear interpolation, one-shot or looped, 16 voices.
#ifndef SAMPLER_CORE_HPP_INCLUDED
#define SAMPLER_CORE_HPP_INCLUDED

#include <array>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "dr_wav.h"

struct SamplerPad {
    std::vector<float> data;  // mono, the whole file
    uint32_t fileRate = 48000;
    uint32_t start = 0;
    uint32_t end = 0;
    int root = 36;
    float gain = 1.0f;
    float pan = 0.0f;
    float pitch = 0.0f;
    int group = 0;  // playback group: only one pad of a non-zero group sounds at a time
    bool loop = false;
    bool reverse = false;
    bool gate = false;  // play only while the note is held; one-shot plays to the end

    bool loaded() const { return end > start && end <= data.size(); }
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
    void reload() { load(); }

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
        double pos = 0.0;
        double step = 1.0;
        float gain = 1.0f;
        float panL = 0.707107f;
        float panR = 0.707107f;
        float release = 1.0f;
        bool loop = false;
        bool gate = false;
        bool releasing = false;
        bool active = false;
        uint64_t age = 0;
    };

    void load();

    double rate = 48000.0;
    int slot = 0;
    std::shared_ptr<const SamplerBank> bank;
    std::array<Voice, 16> voices;
    uint64_t nextAge = 0;
    float releaseStep = 0.01f;  // one sample's share of the 5 ms note-off fade
};

#endif // SAMPLER_CORE_HPP_INCLUDED
