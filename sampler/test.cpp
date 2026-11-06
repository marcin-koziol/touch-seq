// SPDX-License-Identifier: GPL-3.0-or-later
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
    std::ofstream stretch(root / "sampler" / "sampler-11.txt");
    stretch << "mono 0\n";
    stretch << "pad 0 tone.wav 0 48000 36 1.0 0.0 0 0 0 0 0 0\n";
    stretch << "pad 1 tone.wav 0 48000 37 1.0 0.0 12 0 0 0 0 1\n";   // +12, stretch: same length
    stretch << "pad 2 tone.wav 0 48000 38 1.0 0.0 12 0 0 0 0 0\n";   // +12, resample: half the length
    stretch.close();
    std::ofstream down(root / "sampler" / "sampler-15.txt");
    down << "mono 0\n";
    down << "pad 0 tone.wav 0 48000 36 1.0 0.0 -12 0 0 0 0 1\n";  // -12, stretch: an octave down, still 48000 frames
    down.close();
    std::ofstream envelopes(root / "sampler" / "sampler-12.txt");
    envelopes << "mono 0\n";
    envelopes << "pad 0 tone.wav 0 48000 36 1.0 0.0 0 0 0 0 0 0 0.5 0 1 0 0 0 1 0 20000 0 0\n";  // slow attack
    envelopes << "pad 1 tone.wav 0 48000 37 1.0 0.0 0 0 0 0 0 0 0 0 1 0 0 0 1 0 200 0 0\n";    // 200 Hz low-pass
    envelopes.close();
    std::ofstream lfos(root / "sampler" / "sampler-13.txt");
    lfos << "mono 0\n";
    lfos << "pad 0 tone.wav 0 48000 36 1.0 0.0 0 0 0 0 0 0 0 0 1 0 0 0 1 0 20000 0 0 0 10 1 0\n";
    lfos.close();
    std::ofstream synced(root / "sampler" / "sampler-14.txt");
    synced << "mono 0\n";
    synced << "pad 0 tone.wav 0 48000 36 1.0 0.0 0 0 0 0 0 0 0 0 1 0 0 0 1 0 20000 0 0 0 0.1 1 0 1 2\n";
    synced.close();

    setenv("TOUCHSEQ_SAMPLER_DIR", root.c_str(), 1);
    SamplerCore core;
    core.prepare(sr);
    core.setSlot(7);

    std::vector<float> left(sr), right(sr);
    core.process(left.data(), right.data(), uint32_t(left.size()));
    assert(rms(left) < 1e-6);
    std::puts("no note: silent");

    core.noteOn(36, 127);
    core.process(left.data(), right.data(), 1000);
    float head = -1.0f;
    int playing = -2;
    core.playhead(head, playing);
    assert(playing == 0 && head > 0.0f && head < 1.0f);
    core.allNotesOff();
    core.process(left.data(), right.data(), 100);
    core.playhead(head, playing);
    assert(playing == -1);
    std::puts("playhead OK");

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

    // a light map update while a voice plays: it follows the new slice without a gap
    core.setSlot(9);
    core.noteOn(36, 127);
    core.process(left.data(), right.data(), 1000);
    std::ofstream edited(root / "sampler" / "sampler-9.txt");
    edited << "mono 0\n";
    edited << "pad 0 tone.wav 0 12000 36 1.0 0.0 0 0 0 0\n";
    edited << "pad 1 tone.wav 0 24000 37 1.0 0.0 0 1 0 0\n";
    edited.close();
    core.applyMap();
    core.process(left.data(), right.data(), 1);  // the audio thread adopts the pending map
    core.playhead(head, playing);
    assert(playing == 0 && head > 0.04f);  // the shorter slice, not the old one (1000/48000)
    core.process(left.data(), right.data(), 1000);
    assert(core.activeVoices() == 1);
    core.allNotesOff();
    std::puts("map update OK");

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

    core.setSlot(11);  // stretch: PITCH +12 without shortening the slice
    core.noteOn(37, 127);
    core.process(left.data(), right.data(), 24000);
    std::printf("stretch: RMS %.4f, voices %d\n", rms_range(left, 0, 24000), core.activeVoices());
    assert(core.activeVoices() == 1 && rms_range(left, 0, 24000) > 0.01);
    core.process(left.data(), right.data(), 24000);
    // the shifter delays its output by its own latency, so the last frames of the 48000-frame slice follow
    int tail = 0;
    while (core.activeVoices() && tail < 24000) {
        core.process(left.data(), right.data(), 4096);
        tail += 4096;
    }
    std::printf("stretch tail: %d frames, voices %d\n", tail, core.activeVoices());
    assert(core.activeVoices() == 0);  // the full 48000-frame slice, plus the shifter's delay
    core.noteOn(38, 127);  // the same +12 without stretch: twice as fast, half the length
    core.process(left.data(), right.data(), 24000);
    assert(core.activeVoices() == 0);
    core.setSlot(15);  // an octave down, stretch: the same length too
    core.noteOn(36, 127);
    core.process(left.data(), right.data(), 24000);
    std::printf("stretch down: RMS %.4f, voices %d\n", rms_range(left, 0, 24000), core.activeVoices());
    assert(core.activeVoices() == 1 && rms_range(left, 0, 24000) > 0.01);
    core.process(left.data(), right.data(), 24000);
    tail = 0;
    while (core.activeVoices() && tail < 24000) {
        core.process(left.data(), right.data(), 4096);
        tail += 4096;
    }
    assert(core.activeVoices() == 0);
    core.allNotesOff();
    std::puts("stretch OK");

    core.setSlot(12);  // amp/filter envelopes and the low-pass
    core.noteOn(36, 127);
    core.process(left.data(), right.data(), 48000);
    const double attackRms = rms_range(left, 0, 2000), sustainRms = rms_range(left, 20000, 22000);
    std::printf("amp envelope: %.4f -> %.4f\n", attackRms, sustainRms);
    assert(attackRms < sustainRms * 0.3);
    core.allNotesOff();
    core.noteOn(37, 127);
    core.process(left.data(), right.data(), 48000);
    const double filteredRms = rms_range(left, 20000, 22000);
    std::printf("filter: RMS %.4f\n", filteredRms);
    assert(filteredRms < 0.1);  // 440 Hz through a 200 Hz low-pass
    core.allNotesOff();
    std::puts("amp/filter envelope OK");

    core.setSlot(13);  // an amp LFO at 10 Hz, full depth: a tremolo
    core.noteOn(36, 127);
    core.process(left.data(), right.data(), 48000);
    double lfoLow = 1e9, lfoHigh = 0.0;
    for (int w = 0; w + 2400 <= 48000; w += 2400) {
        const double r = rms_range(left, w, w + 2400);
        lfoLow = std::min(lfoLow, r);
        lfoHigh = std::max(lfoHigh, r);
    }
    std::printf("lfo amp: %.4f..%.4f\n", lfoLow, lfoHigh);
    assert(lfoHigh > lfoLow * 3.0);
    core.allNotesOff();
    std::puts("LFO OK");

    core.setSlot(14);  // the same at 1/4 at 120 BPM: 2 Hz
    core.setTempo(120.0f);
    core.noteOn(36, 127);
    core.process(left.data(), right.data(), 48000);
    double syncLow = 1e9, syncHigh = 0.0;
    for (int w = 0; w + 2400 <= 48000; w += 2400) {
        const double r = rms_range(left, w, w + 2400);
        syncLow = std::min(syncLow, r);
        syncHigh = std::max(syncHigh, r);
    }
    std::printf("synced lfo: %.4f..%.4f\n", syncLow, syncHigh);
    assert(syncHigh > syncLow * 3.0);
    core.allNotesOff();
    std::puts("tempo sync OK");

    core.setSlot(8);  // no manifest for this slot
    core.noteOn(36, 127);
    core.process(left.data(), right.data(), 1000);
    assert(rms_range(left, 0, 1000) < 1e-6);
    std::puts("missing slot: silent");

    fs::remove_all(root);
    std::puts("sampler OK");
    return 0;
}
