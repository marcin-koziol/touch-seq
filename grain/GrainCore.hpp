// SPDX-License-Identifier: ISC
// A chromatic granular synth: one slice of a WAV is played by overlapping grains across the keyboard. The
// sample and slice come from Touch-Seq's sampler map (TOUCHSEQ_SAMPLER_DIR/sampler/sampler-<slot>.txt), so
// the existing sampler editor is the source picker; this core adds the grain stream and note tracking.
#ifndef GRAIN_CORE_HPP_INCLUDED
#define GRAIN_CORE_HPP_INCLUDED

#include <array>
#include <cstdint>
#include <memory>
#include <vector>

class GrainCore {
public:
    static constexpr int kVoices = 16;
    static constexpr int kGrains = 48;  // per voice; a high density replaces the oldest slots

    void prepare(double sampleRate);
    void setSlot(int slot);
    void setPad(int pad);
    void reload();

    void setRoot(int note);
    void setKeytrack(float value);
    void setPosition(float value);
    void setSize(float seconds);
    void setDensity(float grainsPerSecond);
    void setSpray(float value);
    void setCoarse(float semitones);
    void setFine(float cents);
    void setReverse(float value);
    void setPan(float value);
    void setLevel(float value);
    void setAttack(float seconds);
    void setDecay(float seconds);
    void setSustain(float value);
    void setRelease(float seconds);
    void setCutoff(float hz);
    void setReso(float value);

    void noteOn(int note, int velocity);
    void noteOff(int note);
    void allNotesOff();
    int activeVoices() const;

    void process(float* left, float* right, uint32_t frames);

private:
    struct Grain {
        double pos = 0.0;      // read position, in the sample's frames
        double step = 1.0;     // frames per output frame (pitch, rate and direction)
        int age = 0;
        int length = 1;
        float panL = 0.70710678f;
        float panR = 0.70710678f;
        bool active = false;
    };

    struct Voice {
        int note = -1;
        bool active = false;
        bool releasing = false;
        float velocity = 1.0f;
        double env = 0.0;
        int stage = 0;  // 0 attack, 1 decay, 2 sustain, 3 release
        double releaseStep = 0.0;
        double spawn = 0.0;
        int next = 0;
        std::array<Grain, kGrains> grains{};
        float ic1L = 0.0f, ic2L = 0.0f, ic1R = 0.0f, ic2R = 0.0f;  // the voice's stereo low-pass
        uint64_t age = 0;
    };

    void loadSample();
    float read(double pos) const;
    void spawn(Voice& voice);
    double ratioFor(int note) const;
    double random();

    double rate = 48000.0;
    int slot = 0;
    int pad = 0;

    int root = 60;
    float keytrack = 1.0f;
    float position = 0.35f;
    float size = 0.08f;
    float density = 30.0f;
    float spray = 0.08f;
    float coarse = 0.0f;
    float fine = 0.0f;
    float reverse = 0.0f;
    float pan = 0.3f;
    float level = 0.8f;
    float attack = 0.01f;
    float decay = 0.2f;
    float sustain = 0.8f;
    float release = 0.4f;
    float cutoff = 16000.0f;
    float reso = 0.1f;
    float norm = 1.0f;  // 1/sqrt(overlapping grains), keeps the level roughly constant

    std::shared_ptr<const std::vector<float>> data;
    uint32_t start = 0, end = 0, fileRate = 48000;
    std::array<Voice, kVoices> voices;
    std::array<float, 1025> window{};
    uint64_t seed = 0x9e3779b97f4a7c15ULL;
    uint64_t nextAge = 0;
};

#endif // GRAIN_CORE_HPP_INCLUDED
