// SPDX-License-Identifier: GPL-3.0-or-later
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

static float lfoValue(int wave, double phase) {
    switch (wave) {
    case 1: return float(1.0 - 4.0 * std::fabs(phase - 0.5));  // triangle
    case 2: return phase < 0.5 ? 1.0f : -1.0f;                  // square
    case 3: return float(2.0 * phase - 1.0);                    // saw
    default: return float(std::sin(2.0 * 3.141592653589793 * phase));
    }
}

static void setPadLfo(SamplerPad& pad, int wave, float rate, float depth, int target, int sync, int div) {
    pad.lfoWave = std::max(0, std::min(3, wave));
    pad.lfoRate = std::max(0.01f, std::min(50.0f, rate));
    pad.lfoDepth = std::max(0.0f, std::min(1.0f, depth));
    pad.lfoTarget = std::max(0, std::min(2, target));
    pad.lfoSync = sync != 0;
    pad.lfoDiv = std::max(0, std::min(6, div));
}

// Cycles per beat for the synced LFO: 1/1 .. 1/16T.
static float lfoRateHz(const SamplerPad& pad, float tempo) {
    static const float divisions[7] = {0.25f, 0.5f, 1.0f, 2.0f, 4.0f, 1.5f, 3.0f};
    const int d = std::max(0, std::min(6, pad.lfoDiv));
    return pad.lfoSync ? (tempo / 60.0f) * divisions[d] : pad.lfoRate;
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
    voices = {};
    for (auto& voice : voices) {  // one shifter per voice: they can overlap and each has its own pitch
        voice.shifter = std::make_unique<RubberBand::RubberBandLiveShifter>(
            size_t(rate), 1, RubberBand::RubberBandLiveShifter::OptionWindowMedium);
        voice.shiftBlock = voice.shifter->getBlockSize();
        voice.shiftIn.assign(voice.shiftBlock, 0.0f);
        voice.shiftOut.assign(voice.shiftBlock, 0.0f);
        voice.shiftRead = voice.shiftBlock;
    }
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
        float gain = 1.0f, pan = 0.0f, pitch = 0.0f, fine = 0.0f;
        int loop = 0, reverse = 0, gate = 0, group = 0, stretch = 0;
        float ampA = 0.0f, ampD = 0.0f, ampS = 1.0f, ampR = 0.0f;
        float fA = 0.0f, fD = 0.0f, fS = 1.0f, fR = 0.0f;
        float cutoff = 20000.0f, reso = 0.0f, envAmt = 0.0f;
        int lfoWave = 0, lfoTarget = 0, lfoSync = 0, lfoDiv = 2;
        float lfoRate = 2.0f, lfoDepth = 0.0f;
        if (!(in >> index >> name >> start >> end >> rootNote >> gain >> pan >> pitch >> loop >> reverse))
            continue;
        in >> gate;     // older manifests have no gate field: one-shot
        in >> group;    // and no group field: no playback group
        in >> stretch;  // and no stretch field: resample (the pitch changes the length)
        in >> ampA >> ampD >> ampS >> ampR >> fA >> fD >> fS >> fR >> cutoff >> reso >> envAmt;
        in >> lfoWave >> lfoRate >> lfoDepth >> lfoTarget >> lfoSync >> lfoDiv;
        in >> fine;  // appended after the LFO: older manifests stop here and keep 0 cents
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
        pad.fine = fine;
        pad.group = group;
        pad.loop = loop != 0;
        pad.reverse = reverse != 0;
        pad.gate = gate != 0;
        pad.stretch = stretch != 0;
        setPadEnvelope(pad, ampA, ampD, ampS, ampR, fA, fD, fS, fR, cutoff, reso, envAmt);
        setPadLfo(pad, lfoWave, lfoRate, lfoDepth, lfoTarget, lfoSync, lfoDiv);
    }

    std::atomic_store(&bank, std::shared_ptr<const SamplerBank>(next));
}

void SamplerCore::setVoiceStep(Voice& voice, const SamplerPad& pad) {
    const double noteRatio = std::pow(2.0, double(voice.note - pad.root) / 12.0);
    const double base = (double(pad.fileRate) / rate) * noteRatio;
    voice.stretch = pad.stretch && voice.shifter != nullptr;
    voice.baseStep = pad.reverse ? -base : base;
    const double semitones = double(pad.pitch) + double(pad.fine) / 100.0;
    voice.step = voice.baseStep * std::pow(2.0, semitones / 12.0);
}

void SamplerCore::resetShift(Voice& voice, const SamplerPad& pad) {
    if (!voice.shifter)
        return;
    voice.shifter->reset();
    voice.shifter->setPitchScale(std::pow(2.0, (double(pad.pitch) + double(pad.fine) / 100.0) / 12.0));
    voice.shiftDelay = double(voice.shifter->getStartDelay());
    voice.shiftRead = voice.shiftBlock;  // fill a block on the first frame
    voice.shiftDrain = false;
    voice.drainLeft = 0;
}

// One frame of the slice at the timeline speed (the note transposition and the file's rate), which is what
// the shifter expects: it adds the pad's PITCH itself. False once a non-looped slice has ended.
bool SamplerCore::nextInput(Voice& voice, const SamplerPad& pad, float& value) {
    value = 0.0f;
    const double start = double(pad.start), end = double(pad.end);
    if (voice.pos >= end || voice.pos < start) {
        if (!voice.loop)
            return false;
        const double span = end - start;
        if (span <= 1.0)
            return false;
        voice.pos = start + std::fmod(voice.pos - start, span);
        if (voice.pos < start)
            voice.pos += span;
    }
    const std::vector<float>& data = *pad.data;
    const int size = int(data.size());
    const int index = int(voice.pos);
    if (index < 0 || index >= size || index + 1 >= size) {
        value = index >= 0 && index < size ? data[index] : 0.0f;
        voice.pos += voice.baseStep;
        return true;
    }
    const float first = data[index];
    value = first + (data[index + 1] - first) * float(voice.pos - index);
    voice.pos += voice.baseStep;
    return true;
}

void SamplerCore::adopt(Voice& voice, const std::shared_ptr<const SamplerBank>& next) {
    const SamplerPad& pad = next->pads[voice.pad];
    if (!pad.loaded()) {
        voice.active = false;
        return;
    }
    const bool wasStretch = voice.stretch;
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
    if (voice.stretch && !wasStretch)
        resetShift(voice, pad);  // this voice just became a stretch voice
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
        float gain = 1.0f, pan = 0.0f, pitch = 0.0f, fine = 0.0f;
        int loop = 0, reverse = 0, gate = 0, group = 0, stretch = 0;
        float ampA = 0.0f, ampD = 0.0f, ampS = 1.0f, ampR = 0.0f;
        float fA = 0.0f, fD = 0.0f, fS = 1.0f, fR = 0.0f;
        float cutoff = 20000.0f, reso = 0.0f, envAmt = 0.0f;
        int lfoWave = 0, lfoTarget = 0, lfoSync = 0, lfoDiv = 2;
        float lfoRate = 2.0f, lfoDepth = 0.0f;
        if (!(in >> index >> name >> start >> end >> rootNote >> gain >> pan >> pitch >> loop >> reverse))
            continue;
        in >> gate;
        in >> group;
        in >> stretch;
        in >> ampA >> ampD >> ampS >> ampR >> fA >> fD >> fS >> fR >> cutoff >> reso >> envAmt;
        in >> lfoWave >> lfoRate >> lfoDepth >> lfoTarget >> lfoSync >> lfoDiv;
        in >> fine;  // appended after the LFO: older manifests stop here and keep 0 cents
        if (index < 0 || index >= kPads)
            continue;
        SamplerPad& pad = next->pads[index];
        pad.file = name;
        pad.root = rootNote;
        pad.gain = gain;
        pad.pan = std::max(-1.0f, std::min(1.0f, pan));
        pad.pitch = pitch;
        pad.fine = fine;
        pad.group = group;
        pad.loop = loop != 0;
        pad.reverse = reverse != 0;
        pad.gate = gate != 0;
        pad.stretch = stretch != 0;
        setPadEnvelope(pad, ampA, ampD, ampS, ampR, fA, fD, fS, fR, cutoff, reso, envAmt);
        setPadLfo(pad, lfoWave, lfoRate, lfoDepth, lfoTarget, lfoSync, lfoDiv);
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
        resetShift(*voice, pad);
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
    voice->lfoPhase = 0.0;
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
    double pos = newest->pos;
    if (newest->stretch) {  // the shifter was fed ahead of what is being heard
        pos -= newest->shiftDelay * newest->baseStep;
        const double span = double(slice.end) - double(slice.start);
        if (newest->loop && span > 1.0) {
            pos = double(slice.start) + std::fmod(pos - double(slice.start), span);
            if (pos < double(slice.start))
                pos += span;
        }
        pos = std::max(double(slice.start), std::min(double(slice.end), pos));
    }
    const double span = double(slice.end) - double(slice.start);
    position = span > 0.0 ? float((pos - double(slice.start)) / span) : 0.0f;
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

            const float lfo = pad.lfoDepth > 0.0f ? lfoValue(pad.lfoWave, voice.lfoPhase) : 0.0f;
            float sample = 0.0f;
            if (voice.stretch) {  // the shifter takes the slice at its own speed and returns it at the pad's pitch
                if (voice.shiftRead >= voice.shiftBlock) {  // one fixed block of the shifter at a time
                    const double lfoPitch = pad.lfoTarget == 2 ? 2.0 * double(pad.lfoDepth) * double(lfo) : 0.0;
                    voice.shifter->setPitchScale(
                        std::exp2((double(pad.pitch) + double(pad.fine) / 100.0 + lfoPitch) / 12.0));
                    for (size_t i = 0; i < voice.shiftBlock; ++i) {
                        float value = 0.0f;
                        if (!voice.shiftDrain && !nextInput(voice, pad, value)) {
                            voice.shiftDrain = true;  // the slice ended; the shifter still holds its tail
                            voice.drainLeft = int(voice.shiftDelay) + 2 * int(voice.shiftBlock);
                        }
                        voice.shiftIn[i] = value;
                    }
                    const float* input[1] = {voice.shiftIn.data()};
                    float* output[1] = {voice.shiftOut.data()};
                    voice.shifter->shift(input, output);
                    voice.shiftRead = 0;
                }
                sample = voice.shiftOut[voice.shiftRead++];
            } else {
                const std::vector<float>& data = *pad.data;
                const int size = int(data.size());
                double readStep = voice.step;
                if (pad.lfoTarget == 2 && pad.lfoDepth > 0.0f)  // pitch LFO, in semitones
                    readStep *= std::exp2(double(pad.lfoDepth) * 2.0 * double(lfo) / 12.0);
                const int index = int(voice.pos);
                if (index < 0 || index >= size) {
                    voice.active = false;
                    continue;
                }
                sample = data[index];
                if (index + 1 < size)
                    sample += (data[index + 1] - sample) * float(voice.pos - index);
                voice.pos += readStep;
            }
            if (pad.lfoDepth > 0.0f) {
                voice.lfoPhase += double(lfoRateHz(pad, tempo)) / rate;
                if (voice.lfoPhase >= 1.0)
                    voice.lfoPhase -= 1.0;
            }

            if (!voice.stretch) {  // a stretch voice wraps and ends where it feeds the shifter (nextInput)
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
            }

            const double stepAbs = std::fabs(voice.stretch ? voice.baseStep : voice.step);
            const double span = double(pad.end - pad.start);
            // the shifter's output lags its input: move the envelope by the same delay
            const double audible = voice.stretch ? std::max(0.0, voice.clock - voice.shiftDelay) : voice.clock;
            const float t = span > 0.0 ? float(audible * stepAbs / span) : 0.0f;
            const bool withRelease = !voice.loop;  // a loop holds sustain; a one-shot releases at its end
            float amp = pad.amp.value(t, withRelease);
            if (pad.lfoTarget == 0 && pad.lfoDepth > 0.0f)  // tremolo, never below silence
                amp *= 1.0f - 0.5f * pad.lfoDepth + 0.5f * pad.lfoDepth * lfo;
            if (pad.cutoff < 19900.0f || pad.envAmount > 0.001f ||
                (pad.lfoTarget == 1 && pad.lfoDepth > 0.0f)) {
                const float fenv = pad.filter.value(t, withRelease);
                float octaves = pad.envAmount * fenv;
                if (pad.lfoTarget == 1 && pad.lfoDepth > 0.0f)
                    octaves += 2.0f * pad.lfoDepth * lfo;  // up to two octaves each way
                float cutoff = pad.cutoff * std::pow(2.0f, octaves);
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
            if (voice.stretch && voice.shiftDrain && --voice.drainLeft <= 0)
                voice.active = false;  // the delayed tail is out
        }
        left[n] = std::tanh(outL);
        right[n] = std::tanh(outR);
    }
}
