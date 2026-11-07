// SPDX-License-Identifier: ISC
// DPF wrapper around GrainCore: a chromatic granular synth. Its editor loads a WAV, sets its slice and
// plays it chromatically across the keyboard.
#include "DistrhoPlugin.hpp"
#include "GrainCore.hpp"

START_NAMESPACE_DISTRHO

class GrainPlugin : public Plugin {
public:
    enum Param {
        kSlot, kReload,
        kRoot, kKeytrack, kStart, kEnd, kPosition, kSize, kDensity, kSpray,
        kCoarse, kFine, kReverse, kPan, kLevel,
        kAttack, kDecay, kSustain, kRelease, kCutoff, kReso,
        kParamCount
    };

    GrainPlugin() : Plugin(kParamCount, 0, 0) {
        core.prepare(getSampleRate());
        applyDefaults();
    }

protected:
    const char* getLabel() const override { return "Grain"; }

    const char* getDescription() const override {
        return "A chromatic granular synth: overlapping grains from a WAV slice, played across the keyboard";
    }

    const char* getMaker() const override { return "Touch-Seq"; }
    const char* getLicense() const override { return "ISC"; }
    uint32_t getVersion() const override { return d_version(1, 0, 0); }
    int64_t getUniqueId() const override { return d_cconst('T', 'S', 'G', 'r'); }

    void initParameter(uint32_t index, Parameter& p) override {
        switch (index) {
        case kSlot:
            p.name = "Slot"; p.symbol = "slot"; p.hints = kParameterIsInteger;
            p.ranges = ParameterRanges(0.0f, 0.0f, 1023.0f);
            break;
        case kReload:
            p.name = "Reload"; p.symbol = "reload"; p.hints = kParameterIsInteger;
            p.ranges = ParameterRanges(0.0f, 0.0f, 1000000.0f);
            break;
        case kRoot:
            p.name = "Root"; p.symbol = "root"; p.hints = kParameterIsInteger;
            p.ranges = ParameterRanges(60.0f, 0.0f, 127.0f);
            break;
        case kKeytrack:
            p.name = "Keytrack"; p.symbol = "keytrack"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(1.0f, 0.0f, 1.0f);
            break;
        case kStart:
            p.name = "Start"; p.symbol = "start"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(0.0f, 0.0f, 1.0f);
            break;
        case kEnd:
            p.name = "End"; p.symbol = "end"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(1.0f, 0.0f, 1.0f);
            break;
        case kPosition:
            p.name = "Position"; p.symbol = "position"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(0.35f, 0.0f, 1.0f);
            break;
        case kSize:
            p.name = "Size"; p.symbol = "size"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(0.08f, 0.005f, 0.5f);
            break;
        case kDensity:
            p.name = "Density"; p.symbol = "density"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(30.0f, 1.0f, 100.0f);
            break;
        case kSpray:
            p.name = "Spray"; p.symbol = "spray"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(0.08f, 0.0f, 1.0f);
            break;
        case kCoarse:
            p.name = "Coarse"; p.symbol = "coarse"; p.hints = kParameterIsInteger;
            p.ranges = ParameterRanges(0.0f, -36.0f, 36.0f);
            break;
        case kFine:
            p.name = "Fine"; p.symbol = "fine"; p.hints = kParameterIsInteger;
            p.ranges = ParameterRanges(0.0f, -100.0f, 100.0f);
            break;
        case kReverse:
            p.name = "Reverse"; p.symbol = "reverse"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(0.0f, 0.0f, 1.0f);
            break;
        case kPan:
            p.name = "Pan Spread"; p.symbol = "pan_spread"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(0.3f, 0.0f, 1.0f);
            break;
        case kLevel:
            p.name = "Level"; p.symbol = "level"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(2.0f, 0.0f, 4.0f);
            break;
        case kAttack:
            p.name = "Attack"; p.symbol = "attack"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(0.01f, 0.001f, 2.0f);
            break;
        case kDecay:
            p.name = "Decay"; p.symbol = "decay"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(0.2f, 0.001f, 2.0f);
            break;
        case kSustain:
            p.name = "Sustain"; p.symbol = "sustain"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(0.8f, 0.0f, 1.0f);
            break;
        case kRelease:
            p.name = "Release"; p.symbol = "release"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(0.4f, 0.01f, 4.0f);
            break;
        case kCutoff:
            p.name = "Cutoff"; p.symbol = "cutoff"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(16000.0f, 20.0f, 20000.0f);
            break;
        case kReso:
            p.name = "Reso"; p.symbol = "reso"; p.hints = kParameterIsAutomatable;
            p.ranges = ParameterRanges(0.1f, 0.0f, 1.0f);
            break;
        default:
            break;
        }
    }

    float getParameterValue(uint32_t index) const override {
        switch (index) {
        case kSlot: return float(slot);
        case kReload: return float(reloads);
        case kRoot: return float(root);
        case kKeytrack: return keytrack;
        case kStart: return start;
        case kEnd: return end;
        case kPosition: return position;
        case kSize: return size;
        case kDensity: return density;
        case kSpray: return spray;
        case kCoarse: return coarse;
        case kFine: return fine;
        case kReverse: return reverse;
        case kPan: return pan;
        case kLevel: return level;
        case kAttack: return attack;
        case kDecay: return decay;
        case kSustain: return sustain;
        case kRelease: return release;
        case kCutoff: return cutoff;
        case kReso: return reso;
        default: return 0.0f;
        }
    }

    void setParameterValue(uint32_t index, float value) override {
        switch (index) {
        case kSlot:
            slot = int(value + 0.5f);
            core.setSlot(slot);
            break;
        case kReload:
            reloads = int(value + 0.5f);
            core.reload();
            break;
        case kRoot: root = int(value + 0.5f); core.setRoot(root); break;
        case kKeytrack: keytrack = value; core.setKeytrack(value); break;
        case kStart:
            start = std::max(0.0f, std::min(1.0f, value));
            core.setRegion(start, end);
            break;
        case kEnd:
            end = std::max(0.0f, std::min(1.0f, value));
            core.setRegion(start, end);
            break;
        case kPosition: position = value; core.setPosition(value); break;
        case kSize: size = value; core.setSize(value); break;
        case kDensity: density = value; core.setDensity(value); break;
        case kSpray: spray = value; core.setSpray(value); break;
        case kCoarse: coarse = value; core.setCoarse(value); break;
        case kFine: fine = value; core.setFine(value); break;
        case kReverse: reverse = value; core.setReverse(value); break;
        case kPan: pan = value; core.setPan(value); break;
        case kLevel: level = value; core.setLevel(value); break;
        case kAttack: attack = value; core.setAttack(value); break;
        case kDecay: decay = value; core.setDecay(value); break;
        case kSustain: sustain = value; core.setSustain(value); break;
        case kRelease: release = value; core.setRelease(value); break;
        case kCutoff: cutoff = value; core.setCutoff(value); break;
        case kReso: reso = value; core.setReso(value); break;
        default: break;
        }
    }

    void activate() override { core.allNotesOff(); }
    void sampleRateChanged(double newSampleRate) override {
        core.prepare(newSampleRate);
        applyValues();
    }

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
    void applyDefaults() {
        root = 60;
        keytrack = 1.0f;
        start = 0.0f;
        end = 1.0f;
        position = 0.35f;
        size = 0.08f;
        density = 30.0f;
        spray = 0.08f;
        coarse = 0.0f;
        fine = 0.0f;
        reverse = 0.0f;
        pan = 0.3f;
        level = 2.0f;
        attack = 0.01f;
        decay = 0.2f;
        sustain = 0.8f;
        release = 0.4f;
        cutoff = 16000.0f;
        reso = 0.1f;
        applyValues();
    }

    void applyValues() {
        core.setRoot(root);
        core.setKeytrack(keytrack);
        core.setRegion(start, end);
        core.setPosition(position);
        core.setSize(size);
        core.setDensity(density);
        core.setSpray(spray);
        core.setCoarse(coarse);
        core.setFine(fine);
        core.setReverse(reverse);
        core.setPan(pan);
        core.setLevel(level);
        core.setAttack(attack);
        core.setDecay(decay);
        core.setSustain(sustain);
        core.setRelease(release);
        core.setCutoff(cutoff);
        core.setReso(reso);
    }

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

    GrainCore core;
    int slot = 0;
    int reloads = 0;
    int root = 60;
    float keytrack = 1.0f, start = 0.0f, end = 1.0f;
    float position = 0.35f, size = 0.08f, density = 30.0f, spray = 0.08f;
    float coarse = 0.0f, fine = 0.0f, reverse = 0.0f, pan = 0.3f;
    float level = 2.0f, attack = 0.01f, decay = 0.2f, sustain = 0.8f, release = 0.4f;
    float cutoff = 16000.0f, reso = 0.1f;

    DISTRHO_DECLARE_NON_COPYABLE_WITH_LEAK_DETECTOR(GrainPlugin)
};

Plugin* createPlugin() { return new GrainPlugin(); }

END_NAMESPACE_DISTRHO
