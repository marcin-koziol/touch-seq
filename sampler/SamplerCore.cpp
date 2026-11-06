// SPDX-License-Identifier: ISC
#define DR_WAV_IMPLEMENTATION
#include "dr_wav.h"

#include "SamplerCore.hpp"

#include <algorithm>
#include <cstdlib>
#include <fstream>
#include <sstream>

// One WAV file as mono float samples; nullptr when it cannot be read. Shared by a full load and a light map
// update, which reuses the data it already has.
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

static void setPadEnvelope(SamplerPad& pad, float aA, float aD, float aS, float aR,
                           float fA, float fD, float fS, float fR,
                           float cutoff, float reso, float envAmount) {
    auto clamp01 = [](float v) { return std::max(0.0f, std::min(1.0f, v)); };
    pad.amp = Envelope{clamp01(aA), clamp01(aD), clamp01(aS), clamp01(aR)};
    pad.filter = Envelope{clamp01(fA), clamp01(fD), clamp01(fS), clamp01(fR)};
    pad.cutoff = std::max(20.0f, std::min(20000.0f, cutoff));
    pad.reso = clamp01(reso);
    pad.envAmount = std::max(0.0f, std::min(8.0f, envAmount));
}

void SamplerCore::prepare(double sampleRate) {
    rate = sampleRate > 1000.0 ? sampleRate : 48000.0;
    releaseStep = 1.0f / (0.005f * float(rate));  // 5 ms fade on note-off
    for (int i = 0; i <= 1024; ++i)
        window[i] = 0.5f * (1.0f - std::cos(2.0 * 3.141592653589793 * i / 1024.0));
    voices = {};
}

void SamplerCore::setSlot(int newSlot) {
    slot = std::max(0, std::min(1023, newSlot));
    load();
}

void SamplerCore::load() {
    const char* root = std::getenv("TOUCHSEQ_SAMPLER_DIR");
    if (root == nullptr || root[0] == '\0') {
        std::atomic_store(&bank, std::shared_ptr<const SamplerBank>());
        return;
    }

    const std::string dir = root;
    const std::string manifest = dir + "/sampler/sampler-" + std::to_string(slot) + ".txt";
    std::ifstream file(manifest);
    if (!file) {
        std::atomic_store(&bank, std::shared_ptr<const SamplerBank>());
        return;
    }

    auto next = std::make_shared<SamplerBank>();
    std::string line;
    while (std::getline(file, line)) {
        std::istringstream in(line);
        std::string tag;
        in >> tag;
        if (tag == "mono") {
            int value = 0;
            if (in >> value)
                next->mono = value != 0;
            continue;
        }
        if (tag != "pad")
            continue;

        int index = -1;
        std::string name;
        uint64_t start = 0, end = 0;
        int rootNote = 36;
        float gain = 1.0f, pan = 0.0f, pitch = 0.0f;
        int loop = 0, reverse = 0, gate = 0, group = 0, stretch = 0;
        float ampA = 0.0f, ampD = 0.0f, ampS = 1.0f, ampR = 0.0f;
        float fA = 0.0f, fD = 0.0f, fS = 1.0f, fR = 0.0f;
        float cutoff = 20000.0f, reso = 0.0f, envAmt = 0.0f;
        if (!(in >> index >> name >> start >> end >> rootNote >> gain >> pan >> pitch >> loop >> reverse))
            continue;
        in >> gate;     // older manifests have no gate field: one-shot
        in >> group;    // and no group field: no playback group
        in >> stretch;  // and no stretch field: resample (the pitch changes the length)
        in >> ampA >> ampD >> ampS >> ampR >> fA >> fD >> fS >> fR >> cutoff >> reso >> envAmt;
        if (index < 0 || index >= kPads)
            continue;

        uint32_t fileRate = 48000;
        auto samples = readSample(dir + "/sampler/" + name, fileRate);
        if (!samples)
            continue;
        const uint64_t frames = samples->size();

        SamplerPad& pad = next->pads[index];
        pad.file = name;
        pad.fileRate = fileRate;
        pad.data = samples;
        pad.start = uint32_t(std::min<uint64_t>(start, frames));
        pad.end = uint32_t(std::min<uint64_t>(end, frames));
        if (pad.end <= pad.start)
            pad.end = uint32_t(frames);
        pad.root = rootNote;
        pad.gain = gain;
        pad.pan = std::max(-1.0f, std::min(1.0f, pan));
        pad.pitch = pitch;
        pad.group = group;
        pad.loop = loop != 0;
        pad.reverse = reverse != 0;
        pad.gate = gate != 0;
        pad.stretch = stretch != 0;
        setPadEnvelope(pad, ampA, ampD, ampS, ampR, fA, fD, fS, fR, cutoff, reso, envAmt);
    }

    std::atomic_store(&bank, std::shared_ptr<const SamplerBank>(next));
}

void SamplerCore::setVoiceStep(Voice& voice, const SamplerPad& pad) {
    const double noteRatio = std::pow(2.0, double(voice.note - pad.root) / 12.0);
    const double base = (double(pad.fileRate) / rate) * noteRatio;
    voice.stretch = pad.stretch;
    voice.baseStep = pad.reverse ? -base : base;
    voice.grainStep = voice.baseStep * std::pow(2.0, double(pad.pitch) / 12.0);
    voice.step = voice.grainStep;  // without stretch the grain step is the playback step
    voice.grainLen = std::max(64.0, 0.05 * rate);
}

void SamplerCore::resetGrains(Voice& voice) {
    voice.grainPos[0] = voice.grainPos[1] = voice.pos;
    voice.grainAge[0] = 0.0;
    voice.grainAge[1] = voice.grainLen * 0.5;
}

void SamplerCore::adopt(Voice& voice, const std::shared_ptr<const SamplerBank>& next) {
    const SamplerPad& pad = next->pads[voice.pad];
    if (!pad.loaded()) {
        voice.active = false;
        return;
    }
    voice.bank = next;
    setVoiceStep(voice, pad);
    voice.gain = pad.gain * voice.velocityGain;
    const float angle = (pad.pan + 1.0f) * 0.78539816339f;  // (pan + 1) * pi/4
    voice.panL = std::cos(angle);
    voice.panR = std::sin(angle);
    voice.loop = pad.loop;
    voice.gate = pad.gate;
    if (voice.pos < double(pad.start))
        voice.pos = double(pad.start);
    if (voice.pos >= double(pad.end))
        voice.pos = pad.loop ? double(pad.start) : double(pad.end) - 1.0;
    if (voice.stretch)
        resetGrains(voice);
}

void SamplerCore::applyMap() {
    const char* root = std::getenv("TOUCHSEQ_SAMPLER_DIR");
    auto current = std::atomic_load(&bank);
    if (root == nullptr || root[0] == '\0') {
        std::atomic_store(&bank, std::shared_ptr<const SamplerBank>());
        std::atomic_store(&pending, std::shared_ptr<const SamplerBank>());
        return;
    }
    const std::string dir = root;
    const std::string manifest = dir + "/sampler/sampler-" + std::to_string(slot) + ".txt";
    std::ifstream file(manifest);
    if (!file) {
        std::atomic_store(&bank, std::shared_ptr<const SamplerBank>());
        std::atomic_store(&pending, std::shared_ptr<const SamplerBank>());
        return;
    }
    auto next = std::make_shared<SamplerBank>();
    std::string line;
    while (std::getline(file, line)) {
        std::istringstream in(line);
        std::string tag;
        in >> tag;
        if (tag == "mono") {
            int value = 0;
            if (in >> value)
                next->mono = value != 0;
            continue;
        }
        if (tag != "pad")
            continue;
        int index = -1;
        std::string name;
        uint64_t start = 0, end = 0;
        int rootNote = 36;
        float gain = 1.0f, pan = 0.0f, pitch = 0.0f;
        int loop = 0, reverse = 0, gate = 0, group = 0, stretch = 0;
        float ampA = 0.0f, ampD = 0.0f, ampS = 1.0f, ampR = 0.0f;
        float fA = 0.0f, fD = 0.0f, fS = 1.0f, fR = 0.0f;
        float cutoff = 20000.0f, reso = 0.0f, envAmt = 0.0f;
        if (!(in >> index >> name >> start >> end >> rootNote >> gain >> pan >> pitch >> loop >> reverse))
            continue;
        in >> gate;
        in >> group;
        in >> stretch;
        in >> ampA >> ampD >> ampS >> ampR >> fA >> fD >> fS >> fR >> cutoff >> reso >> envAmt;
        if (index < 0 || index >= kPads)
            continue;
        SamplerPad& pad = next->pads[index];
        pad.file = name;
        pad.root = rootNote;
        pad.gain = gain;
        pad.pan = std::max(-1.0f, std::min(1.0f, pan));
        pad.pitch = pitch;
        pad.group = group;
        pad.loop = loop != 0;
        pad.reverse = reverse != 0;
        pad.gate = gate != 0;
        pad.stretch = stretch != 0;
        setPadEnvelope(pad, ampA, ampD, ampS, ampR, fA, fD, fS, fR, cutoff, reso, envAmt);
        if (current && current->pads[index].file == name && current->pads[index].data) {
            pad.data = current->pads[index].data;  // the same sample: keep it, no gap
            pad.fileRate = current->pads[index].fileRate;
        } else {
            uint32_t fileRate = 48000;
            pad.data = readSample(dir + "/sampler/" + name, fileRate);
            pad.fileRate = fileRate;
        }
        const uint64_t frames = pad.data ? pad.data->size() : 0;
        pad.start = uint32_t(std::min<uint64_t>(start, frames));
        pad.end = uint32_t(std::min<uint64_t>(end, frames));
        if (pad.end <= pad.start)
            pad.end = uint32_t(frames);
    }
    std::atomic_store(&bank, std::shared_ptr<const SamplerBank>(next));
    std::atomic_store(&pending, std::shared_ptr<const SamplerBank>(next));  // the audio thread points playing voices at it
}

void SamplerCore::noteOn(int note, int velocity) {
    const int index = note - kNoteBase;
    if (index < 0 || index >= kPads)
        return;

    auto current = std::atomic_load(&bank);
    if (!current || !current->pads[index].loaded())
        return;

    const SamplerPad& pad = current->pads[index];
    if (current->mono) {  // one pad at a time: the new pad cuts whatever was still playing
        for (auto& candidate : voices)
            candidate.active = false;
    } else {  // a pad is monophonic by itself, and a playback group holds one voice
        for (auto& candidate : voices) {
            if (!candidate.active)
                continue;
            if (candidate.pad == index ||
                (pad.group && current->pads[candidate.pad].group == pad.group))
                candidate.active = false;
        }
    }

    Voice* voice = nullptr;
    for (auto& candidate : voices) {
        if (!candidate.active) {
            voice = &candidate;
            break;
        }
    }
    if (voice == nullptr) {  // steal the oldest
        voice = &voices[0];
        for (auto& candidate : voices) {
            if (candidate.age < voice->age)
                voice = &candidate;
        }
    }

    voice->bank = current;
    voice->pad = index;
    voice->note = note;
    voice->pos = pad.reverse ? double(pad.end) - 1.0 : double(pad.start);
    setVoiceStep(*voice, pad);
    if (voice->stretch)
        resetGrains(*voice);
    const float velocityGain = 0.2f + 0.8f * std::max(0.0f, std::min(1.0f, velocity / 127.0f));
    voice->velocityGain = velocityGain;
    voice->gain = pad.gain * velocityGain;
    const float angle = (pad.pan + 1.0f) * 0.78539816339f;  // (pan + 1) * pi/4
    voice->panL = std::cos(angle);
    voice->panR = std::sin(angle);
    voice->loop = pad.loop;
    voice->gate = pad.gate;
    voice->releasing = false;
    voice->release = 1.0f;
    voice->clock = 0.0;
    voice->ic1eq = voice->ic2eq = 0.0f;
    voice->active = true;
    voice->age = ++nextAge;
}

void SamplerCore::noteOff(int note) {
    const int index = note - kNoteBase;
    for (auto& voice : voices) {
        if (voice.active && voice.pad == index && (voice.loop || voice.gate))
            voice.releasing = true;  // fade out instead of clicking
    }
}

void SamplerCore::allNotesOff() {
    for (auto& voice : voices)
        voice.active = false;
}

int SamplerCore::activeVoices() const {
    int count = 0;
    for (const auto& voice : voices) {
        if (voice.active)
            ++count;
    }
    return count;
}

void SamplerCore::playhead(float& position, int& pad) const {
    const Voice* newest = nullptr;
    for (const auto& voice : voices) {
        if (voice.active && (newest == nullptr || voice.age > newest->age))
            newest = &voice;
    }
    if (newest == nullptr || newest->bank == nullptr) {
        position = 0.0f;
        pad = -1;
        return;
    }
    const SamplerPad& slice = newest->bank->pads[newest->pad];
    const double span = double(slice.end) - double(slice.start);
    position = span > 0.0 ? float((newest->pos - double(slice.start)) / span) : 0.0f;
    position = std::max(0.0f, std::min(1.0f, position));
    pad = newest->pad;
}

void SamplerCore::process(float* left, float* right, uint32_t frames) {
    auto pendingMap = std::atomic_exchange(&pending, std::shared_ptr<const SamplerBank>());
    if (pendingMap) {  // a light map update: playing voices follow it without a gap
        for (auto& voice : voices) {
            if (voice.active)
                adopt(voice, pendingMap);
        }
    }
    for (uint32_t n = 0; n < frames; ++n) {
        float outL = 0.0f, outR = 0.0f;
        for (auto& voice : voices) {
            if (!voice.active)
                continue;
            if (voice.releasing) {
                voice.release -= releaseStep;
                if (voice.release <= 0.0f) {
                    voice.active = false;
                    continue;
                }
            }

            const SamplerPad& pad = voice.bank->pads[voice.pad];
            if (!pad.loaded()) {
                voice.active = false;
                continue;
            }

            const std::vector<float>& data = *pad.data;
            const int size = int(data.size());
            float sample = 0.0f;
            if (voice.stretch) {  // two overlapping grains: the pitch moves, the timeline keeps its length
                for (int g = 0; g < 2; ++g) {
                    const double age = voice.grainAge[g];
                    if (age < voice.grainLen) {
                        double read = voice.grainPos[g];
                        if (voice.loop) {
                            const double start = pad.start, end = pad.end, span = end - start;
                            if (span > 1.0) {
                                read = start + std::fmod(read - start, span);
                                if (read < start)
                                    read += span;
                            } else {
                                read = start;
                            }
                        }
                        const int idx = int(read);
                        if (idx >= 0 && idx + 1 < size) {
                            const float s = data[idx] + (data[idx + 1] - data[idx]) * float(read - idx);
                            const float w = window[int((age / voice.grainLen) * 1024.0) & 1023];
                            sample += s * w;
                        }
                        voice.grainPos[g] += voice.grainStep;
                        voice.grainAge[g] = age + 1.0;
                    }
                    if (voice.grainAge[g] >= voice.grainLen) {
                        voice.grainAge[g] = 0.0;
                        voice.grainPos[g] = voice.pos;
                    }
                }
                voice.pos += voice.baseStep;
            } else {
                const int index = int(voice.pos);
                if (index < 0 || index >= size) {
                    voice.active = false;
                    continue;
                }
                sample = data[index];
                if (index + 1 < size)
                    sample += (data[index + 1] - sample) * float(voice.pos - index);
                voice.pos += voice.step;
            }

            if (voice.loop) {
                const double start = pad.start;
                const double end = pad.end;
                if (voice.pos >= end || voice.pos < start) {
                    const double span = end - start;
                    if (span > 1.0) {
                        voice.pos = start + std::fmod(voice.pos - start, span);
                        if (voice.pos < start)
                            voice.pos += span;
                    } else {
                        voice.active = false;
                    }
                }
            } else if (voice.pos >= pad.end || voice.pos < pad.start) {
                voice.active = false;
            }

            const double stepAbs = std::fabs(voice.stretch ? voice.baseStep : voice.step);
            const double span = double(pad.end - pad.start);
            const float t = span > 0.0 ? float(voice.clock * stepAbs / span) : 0.0f;
            const bool withRelease = !voice.loop;  // a loop holds sustain; a one-shot releases at its end
            const float amp = pad.amp.value(t, withRelease);
            if (pad.cutoff < 19900.0f || pad.envAmount > 0.001f) {
                const float fenv = pad.filter.value(t, withRelease);
                float cutoff = pad.cutoff * std::pow(2.0f, pad.envAmount * fenv);
                cutoff = std::max(20.0f, std::min(20000.0f, cutoff));
                const float g = std::tan(3.141592653589793f * cutoff / float(rate));
                const float k = 1.0f / (0.707f + 9.3f * pad.reso);  // Q from 0.707 to 10
                const float a1 = 1.0f / (1.0f + g * (g + k));
                const float a2 = g * a1, a3 = g * a2;
                const float v3 = sample - voice.ic2eq;
                const float v1 = a1 * voice.ic1eq + a2 * v3;
                const float v2 = voice.ic2eq + a2 * voice.ic1eq + a3 * v3;
                voice.ic1eq = 2.0f * v1 - voice.ic1eq;
                voice.ic2eq = 2.0f * v2 - voice.ic2eq;
                sample = v2;
            }
            voice.clock += 1.0;
            const float gain = voice.gain * voice.release * amp;
            outL += sample * gain * voice.panL;
            outR += sample * gain * voice.panR;
        }
        left[n] = std::tanh(outL);
        right[n] = std::tanh(outR);
    }
}
