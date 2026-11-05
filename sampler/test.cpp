// SPDX-License-Identifier: ISC
// The sampler's engine: silence without a manifest, a pad sounds, a one-shot ignores note-off, a looped pad
// stops on note-off, and a missing slot is silent.
#include "SamplerCore.hpp"

#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <vector>

static double rms(const std::vector<float>& v) {
    double sum = 0.0;
    for (float x : v)
        sum += double(x) * double(x);
    return std::sqrt(sum / double(v.size()));
}

static double rms_range(const std::vector<float>& v, size_t from, size_t to) {
    double sum = 0.0;
    for (size_t i = from; i < to; ++i)
        sum += double(v[i]) * double(v[i]);
    return std::sqrt(sum / double(to - from));
}

int main() {
    namespace fs = std::filesystem;
    const fs::path root = fs::temp_directory_path() / "touchseq-sampler-test";
    fs::remove_all(root);
    fs::create_directories(root / "sampler");

    const uint32_t sr = 48000;
    std::vector<drwav_int16> pcm(sr);
    for (uint32_t i = 0; i < sr; ++i)
        pcm[i] = drwav_int16(0.5 * 32767.0 * std::sin(2.0 * 3.141592653589793 * 440.0 * i / sr));

    drwav_data_format format{};
    format.container = drwav_container_riff;
    format.format = DR_WAVE_FORMAT_PCM;
    format.channels = 1;
    format.sampleRate = sr;
    format.bitsPerSample = 16;
    drwav wav;
    const std::string wavPath = (root / "sampler" / "tone.wav").string();
    assert(drwav_init_file_write(&wav, wavPath.c_str(), &format, nullptr));
    assert(drwav_write_pcm_frames(&wav, pcm.size(), pcm.data()) == pcm.size());
    drwav_uninit(&wav);

    std::ofstream manifest(root / "sampler" / "sampler-7.txt");
    manifest << "mono 1\n";
    manifest << "pad 0 tone.wav 0 48000 36 1.0 0.0 0 0 0 0\n";   // one-shot
    manifest << "pad 1 tone.wav 0 24000 37 1.0 0.0 0 1 0 0\n";   // loop
    manifest << "pad 2 tone.wav 0 24000 38 1.0 0.0 0 0 0 1\n";   // gate
    manifest.close();
    std::ofstream poly(root / "sampler" / "sampler-9.txt");
    poly << "mono 0\n";
    poly << "pad 0 tone.wav 0 48000 36 1.0 0.0 0 0 0 0\n";
    poly << "pad 1 tone.wav 0 24000 37 1.0 0.0 0 1 0 0\n";
    poly.close();
    std::ofstream groups(root / "sampler" / "sampler-10.txt");
    groups << "mono 0\n";
    groups << "pad 0 tone.wav 0 24000 36 1.0 0.0 0 0 0 0 0\n";  // no group: overlaps freely
    groups << "pad 1 tone.wav 0 24000 37 1.0 0.0 0 0 0 0 0\n";
    groups << "pad 2 tone.wav 0 24000 38 1.0 0.0 0 0 0 0 2\n";  // playback group 2
    groups << "pad 3 tone.wav 0 24000 39 1.0 0.0 0 0 0 0 2\n";
    groups << "pad 4 tone.wav 0 24000 40 1.0 0.0 0 0 0 0 3\n";  // group 3
    groups.close();

    setenv("TOUCHSEQ_SAMPLER_DIR", root.c_str(), 1);
    SamplerCore core;
    core.prepare(sr);
    core.setSlot(7);

    std::vector<float> left(sr), right(sr);
    core.process(left.data(), right.data(), uint32_t(left.size()));
    assert(rms(left) < 1e-6);
    std::puts("no note: silent");

    core.noteOn(36, 127);
    assert(core.activeVoices() == 1);
    core.process(left.data(), right.data(), uint32_t(left.size()));
    std::printf("pad 0: RMS %.4f\n", rms(left));
    assert(rms(left) > 0.01);
    core.noteOn(36, 127);  // the same pad again: it restarts, it does not layer
    assert(core.activeVoices() == 1);
    core.allNotesOff();

    std::fill(left.begin(), left.end(), 0.0f);
    std::fill(right.begin(), right.end(), 0.0f);
    core.noteOn(36, 127);
    core.process(left.data(), right.data(), 1000);
    core.noteOff(36);  // a one-shot keeps playing
    core.process(left.data(), right.data(), 1000);
    assert(rms_range(left, 0, 2000) > 0.001);
    core.allNotesOff();

    core.noteOn(37, 127);
    core.process(left.data(), right.data(), 1000);
    core.noteOff(37);  // a loop stops on note-off (a 5 ms fade, then silence)
    core.process(left.data(), right.data(), uint32_t(left.size()));
    std::printf("looped pad after note-off: RMS %.4f\n", rms_range(left, 1000, 48000));
    assert(rms_range(left, 1000, 48000) < 0.001);

    core.allNotesOff();
    std::fill(left.begin(), left.end(), 0.0f);
    std::fill(right.begin(), right.end(), 0.0f);
    core.noteOn(38, 127);  // a gate pad: note-off stops it (with a short fade)
    core.process(left.data(), right.data(), 1000);
    core.noteOff(38);
    core.process(left.data(), right.data(), 1000);
    assert(rms_range(left, 0, 1000) > 0.001);
    assert(rms_range(left, 1500, 2000) < 0.001);
    std::puts("gate pad: stops on note-off");

    core.allNotesOff();
    core.noteOn(36, 127);
    core.noteOn(37, 127);
    assert(core.activeVoices() == 1);  // mono: the second pad cuts the first
    core.allNotesOff();

    core.setSlot(9);  // the same pads, but poly
    core.noteOn(36, 127);
    core.noteOn(37, 127);
    assert(core.activeVoices() == 2);  // poly: the pads overlap
    core.allNotesOff();
    std::puts("mono and poly OK");

    core.setSlot(10);  // playback groups: one voice per non-zero group
    core.noteOn(36, 127);
    core.noteOn(37, 127);
    assert(core.activeVoices() == 2);  // pads without a group still overlap
    core.noteOn(38, 127);
    core.noteOn(39, 127);  // the same group: pad 3 cuts pad 2
    assert(core.activeVoices() == 3);
    core.noteOn(40, 127);  // another group does not cut group 2
    assert(core.activeVoices() == 4);
    core.noteOn(38, 127);  // back to pad 2: it cuts pad 3
    assert(core.activeVoices() == 4);
    core.allNotesOff();
    std::puts("playback groups OK");

    core.setSlot(8);  // no manifest for this slot
    core.noteOn(36, 127);
    core.process(left.data(), right.data(), 1000);
    assert(rms(left) < 1e-6);
    std::puts("missing slot: silent");

    fs::remove_all(root);
    std::puts("sampler OK");
    return 0;
}
