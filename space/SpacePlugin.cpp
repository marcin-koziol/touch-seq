// SPDX-License-Identifier: ISC
// DPF wrapper around SpaceCore: a delay and a reverb in one, stereo in/out, no UI (its knobs live in HOST-4).
#include "DistrhoPlugin.hpp"
#include "SpaceCore.hpp"

START_NAMESPACE_DISTRHO

class SpacePlugin : public Plugin {
public:
    enum Param { kMix, kReverb, kSize, kDamp, kDelay, kTime, kFeedback, kTone, kParamCount };

    SpacePlugin() : Plugin(kParamCount, 0, 0) {
        core.prepare(getSampleRate());
    }

protected:
    const char* getLabel() const override { return "Space"; }

    const char* getDescription() const override {
        return "A delay feeding a reverb, for Touch-Seq: eight knobs, no window";
    }

    const char* getMaker() const override { return "Touch-Seq"; }
    const char* getLicense() const override { return "ISC"; }
    uint32_t getVersion() const override { return d_version(1, 0, 0); }
    int64_t getUniqueId() const override { return d_cconst('T', 'S', 'S', 'p'); }

    void initParameter(uint32_t index, Parameter& p) override {
        p.hints = kParameterIsAutomatable;
        switch (index) {
        case kMix: p.name = "Mix"; p.symbol = "mix"; p.ranges = ParameterRanges(0.35f, 0.0f, 1.0f); break;
        case kReverb: p.name = "Reverb"; p.symbol = "reverb"; p.ranges = ParameterRanges(0.6f, 0.0f, 1.0f); break;
        case kSize: p.name = "Size"; p.symbol = "size"; p.ranges = ParameterRanges(0.55f, 0.0f, 1.0f); break;
        case kDamp: p.name = "Damp"; p.symbol = "damp"; p.ranges = ParameterRanges(0.4f, 0.0f, 1.0f); break;
        case kDelay: p.name = "Delay"; p.symbol = "delay"; p.ranges = ParameterRanges(0.35f, 0.0f, 1.0f); break;
        case kTime:
            p.name = "Time"; p.symbol = "time"; p.unit = "ms";
            p.ranges = ParameterRanges(375.0f, 10.0f, 2000.0f);
            break;
        case kFeedback:
            p.name = "Feedback"; p.symbol = "feedback";
            p.ranges = ParameterRanges(0.35f, 0.0f, 0.95f);
            break;
        case kTone: p.name = "Tone"; p.symbol = "tone"; p.ranges = ParameterRanges(0.55f, 0.0f, 1.0f); break;
        }
    }

    float getParameterValue(uint32_t index) const override {
        switch (index) {
        case kMix: return core.mix;
        case kReverb: return core.reverbAmount;
        case kSize: return core.size;
        case kDamp: return core.damp;
        case kDelay: return core.delayAmount;
        case kTime: return core.delayMs;
        case kFeedback: return core.feedback;
        default: return core.tone;
        }
    }

    void setParameterValue(uint32_t index, float value) override {
        switch (index) {
        case kMix: core.mix = value; return;
        case kReverb: core.reverbAmount = value; return;
        case kSize: core.size = value; break;
        case kDamp: core.damp = value; break;
        case kDelay: core.delayAmount = value; return;
        case kTime: core.delayMs = value; break;
        case kFeedback: core.feedback = value; break;
        default: core.tone = value; break;
        }
        core.update();  // the ones that shape the delay line or the tail
    }

    void activate() override { core.reset(); }

    void sampleRateChanged(double sampleRate) override {
        core.prepare(sampleRate);
    }

    void run(const float** inputs, float** outputs, uint32_t frames) override {
        core.process(inputs[0], inputs[1], outputs[0], outputs[1], frames);
    }

private:
    SpaceCore core;
};

Plugin* createPlugin() { return new SpacePlugin(); }

END_NAMESPACE_DISTRHO
