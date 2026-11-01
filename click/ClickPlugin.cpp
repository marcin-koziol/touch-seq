// SPDX-License-Identifier: ISC
// DPF wrapper around ClickCore: stereo in/out (the audio passes through) and a MIDI in, where a note means
// "click". Touch-Seq puts it between the master and the output, so the click is part of the same audio graph
// as the instruments - sample-accurate, and under Carla's care like everything else.
#include "DistrhoPlugin.hpp"
#include "ClickCore.hpp"

START_NAMESPACE_DISTRHO

class ClickPlugin : public Plugin {
public:
    enum Param { kLevel, kParamCount };

    ClickPlugin() : Plugin(kParamCount, 0, 0) {
        core.prepare(getSampleRate());
    }

protected:
    const char* getLabel() const override { return "Click"; }

    const char* getDescription() const override {
        return "The metronome: a click for every beat note Touch-Seq sends, in the audio graph";
    }

    const char* getMaker() const override { return "Touch-Seq"; }
    const char* getLicense() const override { return "ISC"; }
    uint32_t getVersion() const override { return d_version(1, 0, 0); }
    int64_t getUniqueId() const override { return d_cconst('T', 'S', 'C', 'k'); }

    void initParameter(uint32_t index, Parameter& p) override {
        if (index != kLevel)
            return;
        p.name = "Level";
        p.symbol = "level";
        p.hints = kParameterIsAutomatable;
        p.ranges = ParameterRanges(0.7f, 0.0f, 1.0f);
    }

    float getParameterValue(uint32_t index) const override { return core.level; }

    void setParameterValue(uint32_t index, float value) override {
        if (index == kLevel)
            core.level = value;
    }

    void activate() override { core.reset(); }

    void sampleRateChanged(double sampleRate) override {
        core.prepare(sampleRate);
        core.reset();
    }

    void run(const float** inputs, float** outputs, uint32_t frames,
             const MidiEvent* midiEvents, uint32_t midiEventCount) override {
        const float* inLeft = inputs[0];
        const float* inRight = inputs[1];
        float* outLeft = outputs[0];
        float* outRight = outputs[1];
        uint32_t event = 0;
        for (uint32_t i = 0; i < frames; ++i) {
            while (event < midiEventCount && midiEvents[event].frame <= i) {  // sample-accurate: the note's frame
                const MidiEvent& e = midiEvents[event];
                if (e.size >= 3 && (e.data[0] & 0xF0) == 0x90 && e.data[2] > 0)
                    core.noteOn(e.data[1], e.data[2]);
                ++event;
            }
            const float click = core.tick();
            outLeft[i] = inLeft[i] + click;
            outRight[i] = inRight[i] + click;
        }
    }

private:
    ClickCore core;
};

Plugin* createPlugin() { return new ClickPlugin(); }

END_NAMESPACE_DISTRHO
