// SPDX-License-Identifier: ISC
// The grain engine: a chromatic note reads the selected sampler slice, with overlapping windowed grains.
#include "../sampler/dr_wav.h"
#include "GrainCore.hpp"

#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <vector>

static double rms_range(const std::vector<float>& v, size_t from, size_t to) {
    double sum = 0.0;
    for (size_t i = from; i < to; ++i)
        sum += double(v[i]) * double(v[i]);
    return std::sqrt(sum / double(to - from));
}

static double frequency(const std::vector<float>& v, size_t n, double rate) {
    int crossings = 0;
    for (size_t i = 1; i < n; ++i)
        if (v[i - 1] <= 0.0f && v[i] > 0.0f)
            ++crossings;
    return n ? crossings * rate / double(n) : 0.0;
}

static void write_source(const std::filesystem::path& path, uint32_t sampleRate) {
    std::vector<drwav_int16> pcm(sampleRate * 2);
    for (uint32_t i = 0; i < pcm.size(); ++i)
        pcm[i] = drwav_int16(0.5 * 32767.0 * std::sin(2.0 * 3.141592653589793 * 440.0 * i / sampleRate));
    drwav_data_format format{};
    format.container = drwav_container_riff;
    format.format = DR_WAVE_FORMAT_PCM;
    format.channels = 1;
    format.sampleRate = sampleRate;
    format.bitsPerSample = 16;
    drwav wav;
    assert(drwav_init_file_write(&wav, path.string().c_str(), &format, nullptr));
    assert(drwav_write_pcm_frames(&wav, pcm.size(), pcm.data()) == pcm.size());
    drwav_uninit(&wav);
}

static void write_demo(const std::filesystem::path& path, GrainCore& core, uint32_t sampleRate) {
    const uint32_t frames = uint32_t(sampleRate * 3.5);
    std::vector<float> left(frames), right(frames);
    const uint32_t block = 256;
    size_t at = 0;
    const int notes[] = {60, 64, 67, 72};
    for (int note : notes) {
        core.noteOn(note, 110);
        for (uint32_t done = 0; done < uint32_t(sampleRate * 0.65) && at < frames; done += block) {
            const uint32_t n = std::min<uint32_t>(block, frames - uint32_t(at));
            core.process(left.data() + at, right.data() + at, n);
            at += n;
        }
        core.noteOff(note);
    }
    while (at < frames) {
        const uint32_t n = std::min<uint32_t>(block, frames - uint32_t(at));
        core.process(left.data() + at, right.data() + at, n);
        at += n;
    }
    drwav_data_format format{};
    format.container = drwav_container_riff;
    format.format = DR_WAVE_FORMAT_IEEE_FLOAT;
    format.channels = 2;
    format.sampleRate = sampleRate;
    format.bitsPerSample = 32;
    drwav wav;
    assert(drwav_init_file_write(&wav, path.string().c_str(), &format, nullptr));
    std::vector<float> interleaved(frames * 2);
    for (size_t i = 0; i < frames; ++i) {
        interleaved[2 * i] = left[i];
        interleaved[2 * i + 1] = right[i];
    }
    assert(drwav_write_pcm_frames(&wav, frames, interleaved.data()) == frames);
    drwav_uninit(&wav);
}

int main(int argc, char** argv) {
    namespace fs = std::filesystem;
    const fs::path root = fs::temp_directory_path() / "touchseq-grain-test";
    fs::remove_all(root);
    fs::create_directories(root / "sampler");

    const uint32_t sr = 48000;
    write_source(root / "sampler" / "tone.wav", sr);
    std::ofstream manifest(root / "sampler" / "sampler-2.txt");
    manifest << "mono 0\n";
    manifest << "pad 0 tone.wav 0 96000 36 1.0 0.0 0 0 0 0\n";
    manifest.close();
    setenv("TOUCHSEQ_SAMPLER_DIR", root.string().c_str(), 1);

    GrainCore core;
    core.prepare(sr);
    core.setSlot(2);
    core.setPad(0);
    core.setRoot(60);
    core.setKeytrack(1.0f);
    core.setPosition(0.25f);
    core.setSize(0.5f);   // one long grain at a time: the pitch measurement is not smeared by overlap
    core.setDensity(1.0f);
    core.setSpray(0.0f);
    core.setCoarse(0.0f);
    core.setFine(0.0f);
    core.setReverse(0.0f);
    core.setPan(0.0f);
    core.setLevel(1.0f);
    core.setAttack(0.001f);
    core.setDecay(0.01f);
    core.setSustain(1.0f);
    core.setRelease(0.05f);
    core.setCutoff(20000.0f);
    core.setReso(0.0f);

    std::vector<float> left(sr), right(sr);
    core.process(left.data(), right.data(), sr);
    assert(rms_range(left, 0, sr) < 1e-6);
    std::puts("silent without a note");

    core.noteOn(60, 127);
    core.process(left.data(), right.data(), 24000);
    const double rootHz = frequency(left, 24000, sr);
    std::printf("grain root: %.1f Hz, voices %d\n", rootHz, core.activeVoices());
    assert(core.activeVoices() == 1 && rms_range(left, 0, 24000) > 0.01);
    assert(std::abs(rootHz - 440.0) < 8.0);
    core.noteOff(60);
    core.process(left.data(), right.data(), 4800);
    assert(core.activeVoices() == 0);

    core.noteOn(72, 127);  // an octave up, chromatically
    core.process(left.data(), right.data(), 24000);
    const double octaveHz = frequency(left, 24000, sr);
    std::printf("grain octave: %.1f Hz\n", octaveHz);
    assert(std::abs(octaveHz - 880.0) < 16.0);
    core.allNotesOff();

    core.setDensity(30.0f);
    core.setSize(0.08f);
    core.noteOn(60, 100);
    core.noteOn(67, 100);
    assert(core.activeVoices() == 2);
    core.process(left.data(), right.data(), 4800);
    assert(rms_range(left, 0, 4800) > 0.005);
    core.allNotesOff();
    assert(core.activeVoices() == 0);
    std::puts("grain chords OK");

    core.setSlot(99);  // no such sampler map: silent
    std::fill(left.begin(), left.end(), 0.0f);
    std::fill(right.begin(), right.end(), 0.0f);
    core.noteOn(60, 127);
    core.process(left.data(), right.data(), 1024);
    assert(rms_range(left, 0, 1024) < 1e-6);
    core.allNotesOff();
    std::puts("missing slot: silent");

    if (argc > 1) {
        core.setSlot(2);
        core.setDensity(24.0f);
        core.setSize(0.12f);
        core.setSpray(0.18f);
        core.setReverse(0.15f);
        core.setPan(0.5f);
        core.setCutoff(7000.0f);
        core.setReso(0.2f);
        write_demo(argv[1], core, sr);
        std::printf("demo: %s\n", argv[1]);
    }
    std::puts("grain OK");
    return 0;
}
