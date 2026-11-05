// SPDX-License-Identifier: ISC
#define DR_WAV_IMPLEMENTATION
#include "dr_wav.h"

#include "SamplerCore.hpp"

#include <algorithm>
#include <cstdlib>
#include <fstream>
#include <sstream>

void SamplerCore::prepare(double sampleRate) {
    rate = sampleRate > 1000.0 ? sampleRate : 48000.0;
    releaseStep = 1.0f / (0.005f * float(rate));  // 5 ms fade on note-off
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
        int loop = 0, reverse = 0, gate = 0, group = 0;
        if (!(in >> index >> name >> start >> end >> rootNote >> gain >> pan >> pitch >> loop >> reverse))
            continue;
        in >> gate;   // older manifests have no gate field: one-shot
        in >> group;  // and no group field: no playback group
        if (index < 0 || index >= kPads)
            continue;

        drwav wav;
        if (!drwav_init_file(&wav, (dir + "/sampler/" + name).c_str(), nullptr))
            continue;

        const drwav_uint64 frames = wav.totalPCMFrameCount;
        const uint32_t channels = std::max<uint32_t>(1u, wav.channels);
        std::vector<float> interleaved(size_t(frames) * channels);
        drwav_read_pcm_frames_f32(&wav, frames, interleaved.data());

        SamplerPad& pad = next->pads[index];
        pad.fileRate = wav.sampleRate;
        pad.data.resize(size_t(frames));
        for (size_t i = 0; i < size_t(frames); ++i) {
            float sum = 0.0f;
            for (uint32_t c = 0; c < channels; ++c)
                sum += interleaved[i * channels + c];
            pad.data[i] = sum / float(channels);
        }
        drwav_uninit(&wav);

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
    }

    std::atomic_store(&bank, std::shared_ptr<const SamplerBank>(next));
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
    voice->pos = pad.reverse ? double(pad.end) - 1.0 : double(pad.start);
    voice->step = (double(pad.fileRate) / rate)
                  * std::pow(2.0, (double(note - pad.root) + pad.pitch) / 12.0);
    if (pad.reverse)
        voice->step = -voice->step;
    const float velocityGain = 0.2f + 0.8f * std::max(0.0f, std::min(1.0f, velocity / 127.0f));
    voice->gain = pad.gain * velocityGain;
    const float angle = (pad.pan + 1.0f) * 0.78539816339f;  // (pan + 1) * pi/4
    voice->panL = std::cos(angle);
    voice->panR = std::sin(angle);
    voice->loop = pad.loop;
    voice->gate = pad.gate;
    voice->releasing = false;
    voice->release = 1.0f;
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

void SamplerCore::process(float* left, float* right, uint32_t frames) {
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

            const int index = int(voice.pos);
            if (index < 0 || index >= int(pad.data.size())) {
                voice.active = false;
                continue;
            }

            float sample = pad.data[index];
            if (index + 1 < int(pad.data.size()))
                sample += (pad.data[index + 1] - sample) * float(voice.pos - index);
            voice.pos += voice.step;

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

            const float gain = voice.gain * voice.release;
            outL += sample * gain * voice.panL;
            outR += sample * gain * voice.panR;
        }
        left[n] = std::tanh(outL);
        right[n] = std::tanh(outR);
    }
}
