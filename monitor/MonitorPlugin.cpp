// SPDX-License-Identifier: ISC
// DPF wrapper around MonitorCore: two parameters (gain, pan), stereo in/out, no UI. Touch-Seq puts it in an
// audio track that has no effects, so the input has a node: its meter and its fader then work.
#include "DistrhoPlugin.hpp"
#include "MonitorCore.hpp"

START_NAMESPACE_DISTRHO

class MonitorPlugin : public Plugin {
public:
    enum Param { kGain, kPan, kParamCount };

    MonitorPlugin() : Plugin(kParamCount, 0, 0) {
        core.prepare(getSampleRate());
        core.update();
        core.reset();
    }

protected:
    const char* getLabel() const override { return "Monitor"; }
    const char* getDescription() const override { return "Transparent gain and pan for Touch-Seq's audio tracks"; }
    const char* getMaker() const override { return "Touch-Seq"; }
    const char* getLicense() const override { return "ISC"; }
    uint32_t getVersion() const override { return d_version(1, 0, 0); }
    int64_t getUniqueId() const override { return d_cconst('T', 'S', 'M', 'o'); }

    void initParameter(uint32_t index, Parameter& p) override {
        p.hints = kParameterIsAutomatable;
        switch (index) {
        case kGain:
            p.name = "Gain";
            p.symbol = "gain";
            p.unit = "dB";
            p.ranges = ParameterRanges(0.0f, -60.0f, 12.0f);
            break;
        case kPan:
            p.name = "Pan";
            p.symbol = "pan";
            p.ranges = ParameterRanges(0.0f, -1.0f, 1.0f);
            break;
        }
    }

    float getParameterValue(uint32_t index) const override {
        return index == kGain ? core.gainDb : core.pan;
    }

    void setParameterValue(uint32_t index, float value) override {
        if (index == kGain)
            core.gainDb = value;
        else
            core.pan = value;
        core.update();
    }

    void activate() override { core.reset(); }

    void sampleRateChanged(double sampleRate) override {
        core.prepare(sampleRate);
        core.reset();
    }

    void run(const float** inputs, float** outputs, uint32_t frames) override {
        core.process(inputs[0], inputs[1], outputs[0], outputs[1], frames);
    }

private:
    MonitorCore core;
};

Plugin* createPlugin() { return new MonitorPlugin(); }

END_NAMESPACE_DISTRHO
