// SPDX-License-Identifier: ISC
// DPF wrapper around SpaceCore: a delay and a reverb in one, stereo in/out, no UI (its knobs live in HOST-4).
#include "DistrhoPlugin.hpp"
#include "SpaceCore.hpp"

START_NAMESPACE_DISTRHO

namespace {
ParameterEnumerationValue kNoteValues[] = {{0.0f, "1/32"}, {1.0f, "1/16T"}, {2.0f, "1/16"}, {3.0f, "1/8T"},
                                           {4.0f, "1/8"}, {5.0f, "1/4T"}, {6.0f, "1/4"}, {7.0f, "1/2"}};
}  // namespace

class SpacePlugin : public Plugin {
public:
    enum Param { kMix, kReverb, kSize, kDamp, kDelay, kTime, kFeedback, kTone, kSync, kNote, kBpm, kParamCount };
    static constexpr int kNotes = 8;

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
        case kSync:
            p.name = "Sync";
            p.symbol = "sync";
            p.hints |= kParameterIsBoolean;
            p.ranges = ParameterRanges(0.0f, 0.0f, 1.0f);
            break;
        case kNote:
            p.name = "Note";
            p.symbol = "note";
            p.hints |= kParameterIsInteger;
            p.ranges = ParameterRanges(4.0f, 0.0f, float(kNotes - 1));
            p.enumValues.count = kNotes;
            p.enumValues.restrictedMode = true;
            p.enumValues.values = kNoteValues;
            p.enumValues.deleteLater = false;
            break;
        case kBpm:
            p.name = "Bpm";  // kept in step with the project by Touch-Seq: the delay follows the tempo
            p.symbol = "bpm";
            p.ranges = ParameterRanges(120.0f, 20.0f, 300.0f);
            break;
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
        case kTone: return core.tone;
        case kSync: return core.sync ? 1.0f : 0.0f;
        case kNote: return float(core.noteDivision);
        default: return core.bpm;
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
        case kTone: core.tone = value; break;
        case kSync: core.sync = value > 0.5f; break;
        case kNote: core.noteDivision = int(value + 0.5f); break;
        default: core.bpm = value; break;
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
