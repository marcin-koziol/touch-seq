// SPDX-License-Identifier: ISC
// DPF wrapper around SamplerCore: a 16-pad MPC-style sampler, MIDI in, stereo out, no UI (Touch-Seq's sampler
// editor and PLAY-12 pads are its face).
#include "DistrhoPlugin.hpp"
#include "SamplerCore.hpp"

START_NAMESPACE_DISTRHO

class SamplerPlugin : public Plugin {
public:
    enum Param { kSlot, kReload, kLoad, kPlayhead, kPlayPad, kParamCount };

    SamplerPlugin() : Plugin(kParamCount, 0, 0) {
        core.prepare(getSampleRate());
    }

protected:
    const char* getLabel() const override { return "Sampler"; }

    const char* getDescription() const override {
        return "A 16-pad MPC-style sampler: WAV files, chops and pads from Touch-Seq's sampler editor";
    }

    const char* getMaker() const override { return "Touch-Seq"; }
    const char* getLicense() const override { return "ISC"; }
    uint32_t getVersion() const override { return d_version(1, 0, 0); }
    int64_t getUniqueId() const override { return d_cconst('T', 'S', 'S', 'm'); }

    void initParameter(uint32_t index, Parameter& p) override {
        if (index == kSlot) {
            p.name = "Slot";
            p.symbol = "slot";
            p.hints = kParameterIsInteger;
            p.ranges = ParameterRanges(0.0f, 0.0f, 1023.0f);
        } else if (index == kReload) {
            // the editor bumps this after rewriting the manifest: a light reread that reuses the samples
            p.name = "Reload";
            p.symbol = "reload";
            p.hints = kParameterIsInteger;
            p.ranges = ParameterRanges(0.0f, 0.0f, 1000000.0f);
        } else if (index == kLoad) {
            // and this one when the samples themselves changed: a full load of the manifest and the WAVs
            p.name = "Load";
            p.symbol = "load";
            p.hints = kParameterIsInteger;
            p.ranges = ParameterRanges(0.0f, 0.0f, 1000000.0f);
        } else if (index == kPlayhead) {
            // outputs: the editor draws the playhead over the waveform from these
            p.name = "Playhead";
            p.symbol = "playhead";
            p.hints = kParameterIsOutput;
            p.ranges = ParameterRanges(0.0f, 0.0f, 1.0f);
        } else if (index == kPlayPad) {
            p.name = "Playing Pad";
            p.symbol = "playing_pad";
            p.hints = kParameterIsOutput | kParameterIsInteger;
            p.ranges = ParameterRanges(-1.0f, -1.0f, 15.0f);
        }
    }

    float getParameterValue(uint32_t index) const override {
        if (index == kSlot)
            return float(slot);
        if (index == kReload)
            return float(reload);
        if (index == kLoad)
            return float(loads);
        if (index == kPlayhead)
            return playhead;
        if (index == kPlayPad)
            return float(playPad);
        return 0.0f;
    }

    void setParameterValue(uint32_t index, float value) override {
        if (index == kSlot) {
            slot = int(value + 0.5f);
            core.setSlot(slot);
        } else if (index == kReload) {
            reload = int(value + 0.5f);
            core.applyMap();  // the map changed: reuse the samples, let playing voices follow it
        } else if (index == kLoad) {
            loads = int(value + 0.5f);
            core.setSlot(slot);  // the samples changed: reread the manifest and the WAVs
        }
    }

    void activate() override { core.allNotesOff(); }

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
        core.playhead(playhead, playPad);  // for the editor's playhead, read as output parameters
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
        } else if (status == 0xB0 && event.size >= 3) {
            const uint8_t cc = event.data[1] & 0x7F;
            if (cc == 123 || cc == 120)
                core.allNotesOff();
        }
    }

    SamplerCore core;
    int slot = 0;
    int reload = 0;  // bumped by the editor for a light map update
    int loads = 0;   // bumped by the editor when the samples themselves changed
    float playhead = 0.0f;  // output: the newest voice's position within its slice
    int playPad = -1;       // output: that voice's pad, or -1 when nothing is playing

    DISTRHO_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR(SamplerPlugin)
};

Plugin* createPlugin() { return new SamplerPlugin(); }

END_NAMESPACE_DISTRHO
