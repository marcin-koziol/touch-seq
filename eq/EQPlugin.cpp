// SPDX-License-Identifier: ISC
// DPF wrapper around EQCore: eight bands (four parameters each: frequency, gain, width, type) and the
// analyser's spectrum as output parameters - the app has no audio buffers of its own, so the plugin measures
// and the app reads the numbers back (Carla reports parameter values).
#include "DistrhoPlugin.hpp"
#include "EQCore.hpp"

START_NAMESPACE_DISTRHO

namespace {
enum { kPerBand = 4, kBandParams = EQCore::kBandCount * kPerBand, kParamCount = kBandParams + EQCore::kSpectrumCount };
ParameterEnumerationValue kTypeValues[] = {{0.0f, "Bell"}, {1.0f, "Low shelf"}, {2.0f, "High shelf"},
                                           {3.0f, "Low cut"}, {4.0f, "High cut"}};
}  // namespace

class EQPlugin : public Plugin {
public:
    EQPlugin() : Plugin(kParamCount, 0, 0) {
        core.settle();
    }

protected:
    const char* getLabel() const override { return "EQ"; }
    const char* getDescription() const override {
        return "Eight bands for Touch-Seq's touchscreen editor: bell, shelves and cuts, with a spectrum";
    }
    const char* getMaker() const override { return "Touch-Seq"; }
    const char* getLicense() const override { return "ISC"; }
    uint32_t getVersion() const override { return d_version(1, 0, 0); }
    int64_t getUniqueId() const override { return d_cconst('T', 'S', 'E', 'q'); }

    void initParameter(uint32_t index, Parameter& p) override {
        if (index >= kBandParams) {  // the analyser: written by the plugin, read by the app
            const uint32_t band = index - kBandParams;
            p.hints = kParameterIsOutput;
            p.name = String("Spectrum ") + String(int(band) + 1);
            p.symbol = String("s") + String(int(band) + 1);
            p.ranges = ParameterRanges(0.0f, 0.0f, 1.0f);
            return;
        }
        const uint32_t band = index / kPerBand, kind = index % kPerBand;
        const Band& b = core.bands[band];
        const String n = String(int(band) + 1);
        p.hints = kParameterIsAutomatable;
        switch (kind) {
        case 0:
            p.name = String("Band ") + n + " Freq";
            p.symbol = String("b") + n + "_freq";
            p.unit = "Hz";
            p.ranges = ParameterRanges(b.freq, 20.0f, 20000.0f);
            break;
        case 1:
            p.name = String("Band ") + n + " Gain";
            p.symbol = String("b") + n + "_gain";
            p.unit = "dB";
            p.ranges = ParameterRanges(0.0f, -24.0f, 24.0f);
            break;
        case 2:
            p.name = String("Band ") + n + " Width";
            p.symbol = String("b") + n + "_q";
            p.ranges = ParameterRanges(b.q, 0.2f, 10.0f);
            break;
        default:
            p.name = String("Band ") + n + " Type";
            p.symbol = String("b") + n + "_type";
            p.hints |= kParameterIsInteger;
            p.ranges = ParameterRanges(0.0f, 0.0f, 4.0f);
            p.enumValues.count = 5;  // "Bell" .. "High cut", assigned field by field: the struct is not copyable
            p.enumValues.restrictedMode = true;
            p.enumValues.values = kTypeValues;
            p.enumValues.deleteLater = false;
            break;
        }
    }

    float getParameterValue(uint32_t index) const override {
        if (index >= kBandParams) return core.spectrum[index - kBandParams];
        Band& b = const_cast<EQCore&>(core).bands[index / kPerBand];
        switch (index % kPerBand) {
        case 0: return b.freq;
        case 1: return b.gain;
        case 2: return b.q;
        default: return float(b.type);
        }
    }

    void setParameterValue(uint32_t index, float value) override {
        if (index >= kBandParams) return;  // the spectrum is ours to write
        Band& b = core.bands[index / kPerBand];
        switch (index % kPerBand) {
        case 0: b.freq = value; break;
        case 1: b.gain = value; break;
        case 2: b.q = value; break;
        default: b.type = int(value + 0.5f); break;
        }
        core.touch();
    }

    void activate() override { core.reset(); }

    void sampleRateChanged(double sampleRate) override {
        core.prepare(sampleRate);
    }

    void run(const float** inputs, float** outputs, uint32_t frames) override {
        if (inputs[0] != outputs[0]) {
            std::memcpy(outputs[0], inputs[0], sizeof(float) * frames);
            std::memcpy(outputs[1], inputs[1], sizeof(float) * frames);
        }
        core.process(outputs[0], outputs[1], frames);
    }

private:
    EQCore core;
};

Plugin* createPlugin() { return new EQPlugin(); }

END_NAMESPACE_DISTRHO
