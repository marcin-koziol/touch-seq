// SPDX-License-Identifier: ISC
// DPF wrapper around XYFXCore: five parameters, stereo in/out, no UI.
#include "DistrhoPlugin.hpp"
#include "XYFXCore.hpp"

START_NAMESPACE_DISTRHO

class XYFXPlugin : public Plugin {
public:
    enum Param { kEngage, kX, kY, kBpm, kMode, kParamCount };

    XYFXPlugin() : Plugin(kParamCount, 0, 0) { core.prepare(getSampleRate()); }

protected:
    const char* getLabel() const override { return "XYFX"; }
    const char* getDescription() const override { return "Beat repeat (X) and low/high-pass filter (Y) for Touch-Seq's XY pad"; }
    const char* getMaker() const override { return "Touch-Seq"; }
    const char* getLicense() const override { return "ISC"; }
    uint32_t getVersion() const override { return d_version(1, 0, 0); }
    int64_t getUniqueId() const override { return d_cconst('T', 'S', 'x', 'y'); }

    void initParameter(uint32_t index, Parameter& p) override
    {
        p.hints = kParameterIsAutomatable;
        switch (index) {
        case kEngage:
            p.name = "Engage";
            p.symbol = "engage";
            p.hints |= kParameterIsBoolean;
            p.ranges = ParameterRanges(0.0f, 0.0f, 1.0f);
            break;
        case kX:
            p.name = "X";
            p.symbol = "x";
            p.ranges = ParameterRanges(0.5f, 0.0f, 1.0f);
            break;
        case kY:
            p.name = "Y";
            p.symbol = "y";
            p.ranges = ParameterRanges(0.5f, 0.0f, 1.0f);
            break;
        case kBpm:
            p.name = "BPM";
            p.symbol = "bpm";
            p.ranges = ParameterRanges(120.0f, 20.0f, 300.0f);
            break;
        case kMode:  // the pad's left half: triplets or backwards
            p.name = "Mode";
            p.symbol = "mode";
            p.hints |= kParameterIsInteger;
            p.ranges = ParameterRanges(0.0f, 0.0f, 1.0f);
            break;
        }
    }

    float getParameterValue(uint32_t index) const override
    {
        switch (index) {
        case kEngage: return core.engage;
        case kX: return core.x;
        case kY: return core.y;
        case kBpm: return core.bpm;
        case kMode: return core.mode;
        }
        return 0.0f;
    }

    void setParameterValue(uint32_t index, float value) override
    {
        switch (index) {
        case kEngage: core.engage = value; break;
        case kX: core.x = value; break;
        case kY: core.y = value; break;
        case kBpm: core.bpm = value; break;
        case kMode: core.mode = value; break;
        }
    }

    void sampleRateChanged(double newSampleRate) override { core.prepare(newSampleRate); }

    void run(const float** inputs, float** outputs, uint32_t frames) override
    {
        core.process(inputs[0], inputs[1], outputs[0], outputs[1], frames);
    }

private:
    XYFXCore core;

    DISTRHO_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR(XYFXPlugin)
};

Plugin* createPlugin() { return new XYFXPlugin(); }

END_NAMESPACE_DISTRHO
