// SPDX-License-Identifier: ISC
#define DR_WAV_IMPLEMENTATION
#include "../sampler/dr_wav.h"

#include "GrainCore.hpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <sstream>

static std::shared_ptr<const std::vector<float>> readSample(const std::string& path, uint32_t& fileRate) {
    drwav wav;
    if (!drwav_init_file(&wav, path.c_str(), nullptr))
        return nullptr;
    const drwav_uint64 frames = wav.totalPCMFrameCount;
    const uint32_t channels = std::max<uint32_t>(1u, wav.channels);
    std::vector<float> interleaved(size_t(frames) * channels);
    drwav_read_pcm_frames_f32(&wav, frames, interleaved.data());
    auto samples = std::make_shared<std::vector<float>>(size_t(frames));
    for (size_t i = 0; i < size_t(frames); ++i) {
        float sum = 0.0f;
        for (uint32_t c = 0; c < channels; ++c)
            sum += interleaved[i * channels + c];
        (*samples)[i] = sum / float(channels);
    }
    fileRate = wav.sampleRate;
    drwav_uninit(&wav);
    return samples;
}

void GrainCore::prepare(double sampleRate) {
    rate = sampleRate > 1000.0 ? sampleRate : 48000.0;
    for (int i = 0; i <= 1024; ++i)
        window[i] = 0.5f * (1.0f - std::cos(2.0 * 3.141592653589793 * i / 1024.0));
    voices = {};
    norm = float(1.0 / std::sqrt(std::max(1.0, double(density) * double(size))));
}

void GrainCore::setSlot(int value) {
    const int next = std::max(0, std::min(1023, value));
    if (next == slot && data)
        return;
    slot = next;
    allNotesOff();
    loadSample();
}

void GrainCore::setPad(int value) {
    const int next = std::max(0, std::min(15, value));
    if (next == pad && data)
        return;
    pad = next;
    allNotesOff();
    loadSample();
}

void GrainCore::reload() {
    allNotesOff();
    loadSample();
}

void GrainCore::loadSample() {
    data.reset();
    start = end = 0;
    fileRate = 48000;

    const char* root = std::getenv("TOUCHSEQ_SAMPLER_DIR");
    if (root == nullptr || root[0] == '\0')
        return;
    const std::string dir = root;
    std::ifstream file(dir + "/sampler/sampler-" + std::to_string(slot) + ".txt");
    if (!file)
        return;

    std::string line, name;
    uint64_t first = 0, last = 0;
    bool found = false;
    while (std::getline(file, line)) {
        std::istringstream in(line);
        std::string tag;
        in >> tag;
        if (tag != "pad")
            continue;
        int index = -1;
        std::string sampleName;
        uint64_t a = 0, b = 0;
        if (!(in >> index >> sampleName >> a >> b))
            continue;
        if (index == pad) {
            name = sampleName;
            first = a;
            last = b;
            found = true;
            break;
        }
    }
    if (!found)
        return;

    uint32_t rate = 48000;
    auto samples = readSample(dir + "/sampler/" + name, rate);
    if (!samples)
        return;
    const uint64_t frames = samples->size();
    start = uint32_t(std::min<uint64_t>(first, frames));
    end = uint32_t(std::min<uint64_t>(last, frames));
    if (end <= start) {
        start = 0;
        end = uint32_t(frames);
    }
    fileRate = rate;
    data = samples;
}

void GrainCore::setRoot(int note) { root = std::max(0, std::min(127, note)); }
void GrainCore::setKeytrack(float value) { keytrack = std::max(0.0f, std::min(1.0f, value)); }
void GrainCore::setPosition(float value) { position = std::max(0.0f, std::min(1.0f, value)); }
void GrainCore::setSize(float seconds) {
    size = std::max(0.005f, std::min(0.5f, seconds));
    norm = float(1.0 / std::sqrt(std::max(1.0, double(density) * double(size))));
}
void GrainCore::setDensity(float value) {
    density = std::max(1.0f, std::min(100.0f, value));
    norm = float(1.0 / std::sqrt(std::max(1.0, double(density) * double(size))));
}
void GrainCore::setSpray(float value) { spray = std::max(0.0f, std::min(1.0f, value)); }
void GrainCore::setCoarse(float value) { coarse = std::max(-36.0f, std::min(36.0f, value)); }
void GrainCore::setFine(float value) { fine = std::max(-100.0f, std::min(100.0f, value)); }
void GrainCore::setReverse(float value) { reverse = std::max(0.0f, std::min(1.0f, value)); }
void GrainCore::setPan(float value) { pan = std::max(0.0f, std::min(1.0f, value)); }
void GrainCore::setLevel(float value) { level = std::max(0.0f, std::min(1.0f, value)); }
void GrainCore::setAttack(float seconds) { attack = std::max(0.0005f, std::min(2.0f, seconds)); }
void GrainCore::setDecay(float seconds) { decay = std::max(0.0005f, std::min(2.0f, seconds)); }
void GrainCore::setSustain(float value) { sustain = std::max(0.0f, std::min(1.0f, value)); }
void GrainCore::setRelease(float seconds) { release = std::max(0.01f, std::min(4.0f, seconds)); }
void GrainCore::setCutoff(float hz) { cutoff = std::max(20.0f, std::min(20000.0f, hz)); }
void GrainCore::setReso(float value) { reso = std::max(0.0f, std::min(1.0f, value)); }

double GrainCore::random() {
    seed ^= seed << 13;
    seed ^= seed >> 7;
    seed ^= seed << 17;
    return double(seed >> 11) * (1.0 / 9007199254740992.0);
}

double GrainCore::ratioFor(int note) const {
    const double semitones = double(keytrack) * double(note - root) + double(coarse) + double(fine) / 100.0;
    return std::pow(2.0, semitones / 12.0) * (double(fileRate) / rate);
}

float GrainCore::read(double pos) const {
    if (!data || end <= start)
        return 0.0f;
    const double span = double(end) - double(start);
    double x = double(start) + std::fmod(pos - double(start), span);
    if (x < double(start))
        x += span;
    const int index = int(x);
    if (index < 0 || index >= int(data->size()))
        return 0.0f;
    float value = (*data)[size_t(index)];
    if (index + 1 < int(data->size()))
        value += ((*data)[size_t(index + 1)] - value) * float(x - double(index));
    return value;
}

void GrainCore::spawn(Voice& voice) {
    if (!data || end <= start)
        return;
    Grain& grain = voice.grains[size_t(voice.next++ % kGrains)];
    const double span = double(end) - double(start);
    const double where = double(position) + (random() * 2.0 - 1.0) * double(spray);
    grain.pos = double(start) + where * span;
    grain.age = 0;
    grain.length = std::max(2, int(size * rate));
    const double direction = random() < double(reverse) ? -1.0 : 1.0;
    grain.step = ratioFor(voice.note) * direction;
    const float p = float(random() * 2.0 - 1.0) * pan;
    const float angle = (p + 1.0f) * 0.78539816339f;
    grain.panL = std::cos(angle);
    grain.panR = std::sin(angle);
    grain.active = true;
}

void GrainCore::noteOn(int note, int velocity) {
    Voice* voice = nullptr;
    for (auto& candidate : voices) {
        if (!candidate.active) {
            voice = &candidate;
            break;
        }
    }
    if (voice == nullptr) {
        voice = &voices[0];
        for (auto& candidate : voices)
            if (candidate.age < voice->age)
                voice = &candidate;
    }

    *voice = Voice{};
    voice->note = note;
    voice->active = true;
    voice->velocity = 0.2f + 0.8f * std::max(0.0f, std::min(1.0f, float(velocity) / 127.0f));
    voice->spawn = 1.0;  // the first grain right away, not one density period later
    voice->env = attack > 0.0005f ? 0.0 : 1.0;
    voice->stage = attack > 0.0005f ? 0 : 1;
    voice->age = ++nextAge;
}

void GrainCore::noteOff(int note) {
    for (auto& voice : voices) {
        if (!voice.active || voice.note != note || voice.releasing)
            continue;
        voice.releasing = true;
        voice.stage = 3;
        voice.releaseStep = std::max(voice.env, 0.001) / (double(release) * rate);
        if (release <= 0.011f)
            voice.active = false;
    }
}

void GrainCore::allNotesOff() {
    for (auto& voice : voices)
        voice.active = false;
}

int GrainCore::activeVoices() const {
    int count = 0;
    for (const auto& voice : voices)
        if (voice.active)
            ++count;
    return count;
}

void GrainCore::process(float* left, float* right, uint32_t frames) {
    for (uint32_t n = 0; n < frames; ++n) {
        float outL = 0.0f, outR = 0.0f;
        for (auto& voice : voices) {
            if (!voice.active)
                continue;

            if (voice.stage == 0) {
                voice.env += 1.0 / (double(attack) * rate);
                if (voice.env >= 1.0) {
                    voice.env = 1.0;
                    voice.stage = 1;
                }
            } else if (voice.stage == 1) {
                voice.env -= (1.0 - double(sustain)) / (double(decay) * rate);
                if (voice.env <= double(sustain)) {
                    voice.env = double(sustain);
                    voice.stage = 2;
                }
            } else if (voice.stage == 3) {
                voice.env -= voice.releaseStep;
                if (voice.env <= 0.0) {
                    voice.env = 0.0;
                    voice.active = false;
                    continue;
                }
            }

            voice.spawn += double(density) / rate;
            while (voice.spawn >= 1.0) {
                spawn(voice);
                voice.spawn -= 1.0;
            }

            float sumL = 0.0f, sumR = 0.0f;
            for (auto& grain : voice.grains) {
                if (!grain.active)
                    continue;
                const int index = std::max(0, std::min(1024, int(1024.0 * double(grain.age) / double(grain.length))));
                const float sample = read(grain.pos) * window[size_t(index)];
                sumL += sample * grain.panL;
                sumR += sample * grain.panR;
                grain.pos += grain.step;
                if (++grain.age >= grain.length)
                    grain.active = false;
            }

            if (cutoff < 19900.0f || reso > 0.001f) {
                const float g = std::tan(3.141592653589793f * cutoff / float(rate));
                const float k = 1.0f / (0.707f + 9.3f * reso);
                const float a1 = 1.0f / (1.0f + g * (g + k));
                const float a2 = g * a1, a3 = g * a2;
                const float v3L = sumL - voice.ic2L;
                const float v1L = a1 * voice.ic1L + a2 * v3L;
                const float v2L = voice.ic2L + a2 * voice.ic1L + a3 * v3L;
                voice.ic1L = 2.0f * v1L - voice.ic1L;
                voice.ic2L = 2.0f * v2L - voice.ic2L;
                sumL = v2L;
                const float v3R = sumR - voice.ic2R;
                const float v1R = a1 * voice.ic1R + a2 * v3R;
                const float v2R = voice.ic2R + a2 * voice.ic1R + a3 * v3R;
                voice.ic1R = 2.0f * v1R - voice.ic1R;
                voice.ic2R = 2.0f * v2R - voice.ic2R;
                sumR = v2R;
            }

            const float gain = level * voice.velocity * norm * float(voice.env);
            outL += sumL * gain;
            outR += sumR * gain;
        }
        left[n] = std::tanh(outL);
        right[n] = std::tanh(outR);
    }
}
