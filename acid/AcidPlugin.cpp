// SPDX-License-Identifier: GPL-3.0-or-later
// DPF wrapper around AcidCore: a 303-style monophonic bass, MIDI in, stereo out, no UI (its knobs are HOST-4).
// The sound engine is Robin Schmidt's Open303 (MIT), the same engine jc303 uses: see open303/LICENSE.
#include "DistrhoPlugin.hpp"
#include "AcidCore.hpp"

START_NAMESPACE_DISTRHO

namespace {
ParameterEnumerationValue kWaveValues[] = {{0.0f, "Saw"}, {1.0f, "Square"}};
ParameterEnumerationValue kDriveValues[] = {{0.0f, "Off"}, {1.0f, "On"}};
}  // namespace

class AcidPlugin : public Plugin {
public:
    enum Param {
        kOscWave,
        kOscTune,
        kFilterCutoff,
        kFilterReso,
        kFilterEnvMod,
        kFilterDecay,
        kAmpAccent,
        kAmpDrive,
        kAmpSlide,
        kAmpVolume,
        kParamCount
    };

    AcidPlugin() : Plugin(kParamCount, 0, 0) {
        core.prepare(getSampleRate());
    }

protected:
    const char* getLabel() const override { return "Acid303"; }

    const char* getDescription() const override {
        return "A 303-style acid bass: Open303 engine, saw/square, ladder filter, accent, slide and overdrive";
    }

    const char* getMaker() const override { return "Touch-Seq"; }
    const char* getLicense() const override { return "GPL-3.0-OR-LATER"; }
    uint32_t getVersion() const override { return d_version(1, 0, 0); }
    int64_t getUniqueId() const override { return d_cconst('T', 'S', 'A', 'c'); }

    void initParameter(uint32_t index, Parameter& p) override {
        p.hints = kParameterIsAutomatable;
        switch (index) {
        case kOscWave:
            p.name = "Osc Wave";
            p.symbol = "osc_wave";
            p.hints |= kParameterIsBoolean | kParameterIsInteger;
            p.ranges = ParameterRanges(0.0f, 0.0f, 1.0f);
            p.enumValues.count = 2;
            p.enumValues.restrictedMode = true;
            p.enumValues.values = kWaveValues;
            p.enumValues.deleteLater = false;
            break;
        case kOscTune:
            p.name = "Osc Tune";
            p.symbol = "osc_tune";
            p.unit = "st";
            p.hints |= kParameterIsInteger;
            p.ranges = ParameterRanges(0.0f, -12.0f, 12.0f);
            break;
        case kFilterCutoff:
            p.name = "Filter Cutoff";
            p.symbol = "filter_cutoff";
            p.ranges = ParameterRanges(0.25f, 0.0f, 1.0f);
            break;
        case kFilterReso:
            p.name = "Filter Reso";
            p.symbol = "filter_reso";
            p.ranges = ParameterRanges(0.92f, 0.0f, 1.0f);
            break;
        case kFilterEnvMod:
            p.name = "Filter Env Mod";
            p.symbol = "filter_env_mod";
            p.ranges = ParameterRanges(0.60f, 0.0f, 1.0f);
            break;
        case kFilterDecay:
            p.name = "Filter Decay";
            p.symbol = "filter_decay";
            p.ranges = ParameterRanges(0.29f, 0.0f, 1.0f);
            break;
        case kAmpAccent:
            p.name = "Amp Accent";
            p.symbol = "amp_accent";
            p.ranges = ParameterRanges(0.78f, 0.0f, 1.0f);
            break;
        case kAmpDrive:
            p.name = "Amp Drive";
            p.symbol = "amp_drive";
            p.hints |= kParameterIsBoolean | kParameterIsInteger;
            p.ranges = ParameterRanges(0.0f, 0.0f, 1.0f);
            p.enumValues.count = 2;
            p.enumValues.restrictedMode = true;
            p.enumValues.values = kDriveValues;
            p.enumValues.deleteLater = false;
            break;
        case kAmpSlide:
            p.name = "Amp Slide";
            p.symbol = "amp_slide";
            p.ranges = ParameterRanges(0.16f, 0.0f, 1.0f);
            break;
        case kAmpVolume:
            p.name = "Amp Volume";
            p.symbol = "amp_volume";
            p.ranges = ParameterRanges(0.75f, 0.0f, 1.0f);
            break;
        }
    }

    float getParameterValue(uint32_t index) const override {
        switch (index) {
        case kOscWave: return core.wave;
        case kOscTune: return core.tune;
        case kFilterCutoff: return core.cutoff;
        case kFilterReso: return core.resonance;
        case kFilterEnvMod: return core.envMod;
        case kFilterDecay: return core.decay;
        case kAmpAccent: return core.accent;
        case kAmpDrive: return core.overdrive ? 1.0f : 0.0f;
        case kAmpSlide: return core.slide;
        default: return core.volume;
        }
    }

    void setParameterValue(uint32_t index, float value) override {
        switch (index) {
        case kOscWave: core.wave = value > 0.5f ? 1.0f : 0.0f; break;
        case kOscTune: core.tune = value; break;
        case kFilterCutoff: core.cutoff = value; break;
        case kFilterReso: core.resonance = value; break;
        case kFilterEnvMod: core.envMod = value; break;
        case kFilterDecay: core.decay = value; break;
        case kAmpAccent: core.accent = value; break;
        case kAmpDrive: core.setOverdrive(value > 0.5f); return;  // a switch, no coefficients
        case kAmpSlide: core.slide = value; break;
        default: core.volume = value; break;
        }
        core.update();
    }

    void activate() override { core.reset(); }

    void sampleRateChanged(double sampleRate) override { core.prepare(sampleRate); }

    void run(const float**, float** outputs, uint32_t frames,
             const MidiEvent* midiEvents, uint32_t midiEventCount) override {
        float* const outLeft = outputs[0];
        float* const outRight = outputs[1];
        uint32_t pos = 0;
        for (uint32_t i = 0; i < midiEventCount; ++i) {
            const MidiEvent& event = midiEvents[i];
            const uint32_t at = event.frame < frames ? event.frame : frames;
            if (at > pos) {
                core.process(outLeft + pos, outRight + pos, at - pos);
                pos = at;
            }
            handleMidi(event);
        }
        if (pos < frames)
            core.process(outLeft + pos, outRight + pos, frames - pos);
    }

private:
    void handleMidi(const MidiEvent& event) {
        if (event.size < 2)
            return;
        const uint8_t status = event.data[0] & 0xF0;
        if (status == 0x90 && event.size >= 3) {
            const uint8_t note = event.data[1] & 0x7F;
            const uint8_t velocity = event.data[2] & 0x7F;
            if (velocity == 0)
                core.noteOff(note);
            else
                core.noteOn(note, velocity);
        } else if (status == 0x80 && event.size >= 3) {
            core.noteOff(event.data[1] & 0x7F);
        } else if (status == 0xE0 && event.size >= 3) {
            const int value = ((event.data[2] & 0x7F) << 7) | (event.data[1] & 0x7F);
            core.pitchBend((value - 8192) / 8192.0f);
        } else if (status == 0xB0 && event.size >= 3) {
            const uint8_t cc = event.data[1] & 0x7F;
            if (cc == 123 || cc == 120)  // all notes off / all sound off
                core.allNotesOff();
        }
    }

    AcidCore core;

    DISTRHO_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR(AcidPlugin)
};

Plugin* createPlugin() { return new AcidPlugin(); }

END_NAMESPACE_DISTRHO
