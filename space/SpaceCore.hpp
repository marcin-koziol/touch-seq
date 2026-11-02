// SPDX-License-Identifier: ISC
// The DSP of Touch-Seq Space: a stereo delay (with a lowpass in its loop and a ping-pong cross-feed) feeding
// a Freeverb-style reverb - eight damped combs and four allpasses a side - then a dry/wet mix. One plugin for
// the two effects a live set always needs, so nothing has to be found in a plugin list.
#ifndef SPACE_CORE_HPP_INCLUDED
#define SPACE_CORE_HPP_INCLUDED

#include <algorithm>
#include <cmath>
#include <vector>

class SpaceCore {
public:
    static constexpr int kCombs = 8;
    static constexpr int kAllpasses = 4;

    float mix = 0.35f;           // 0 dry .. 1 all wet
    float reverbAmount = 0.6f;   // how much of the reverb
    float size = 0.55f;          // 0 small room .. 1 long hall
    float damp = 0.4f;           // 0 bright .. 1 dark
    float delayAmount = 0.35f;   // how much of the delay
    float delayMs = 375.0f;      // 10 .. 2000 ms, when the delay runs free
    float feedback = 0.35f;      // delay repeats, 0 .. 0.95
    float tone = 0.55f;          // the delay's lowpass: 0 dark .. 1 bright
    bool sync = false;           // the delay time follows the tempo instead of the Time knob
    int noteDivision = 4;        // 0 1/32, 1 1/16T, 2 1/16, 3 1/8T, 4 1/8, 5 1/4T, 6 1/4, 7 1/2
    float bpm = 120.0f;          // 20 .. 300, kept in step with the project by Touch-Seq

    // How long the delay is, in seconds (what the delay line is actually running at).
    float delaySeconds() const { return float(delaySamples) / rate; }

    void prepare(double sampleRate) {
        rate = sampleRate > 1000.0 ? float(sampleRate) : 48000.0f;
        const double scale = rate / 44100.0;
        // Freeverb's tunings, scaled to this sample rate
        static const int combTuning[kCombs] = {1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617};
        static const int allpassTuning[kAllpasses] = {556, 441, 341, 225};
        for (int c = 0; c < kCombs; ++c) {
            combL[c].assign(size_t(std::max(8, int(combTuning[c] * scale))), 0.0f);
            combR[c].assign(size_t(std::max(8, int((combTuning[c] + 23) * scale))), 0.0f);  // a little wider
            combPosL[c] = combPosR[c] = 0;
            combStoreL[c] = combStoreR[c] = 0.0f;
        }
        for (int a = 0; a < kAllpasses; ++a) {
            apL[a].assign(size_t(std::max(4, int(allpassTuning[a] * scale))), 0.0f);
            apR[a].assign(size_t(std::max(4, int((allpassTuning[a] + 23) * scale))), 0.0f);
            apPosL[a] = apPosR[a] = 0;
        }
        delaySize = int(rate * 2.05f);  // two seconds and a little
        delayL.assign(size_t(delaySize), 0.0f);
        delayR.assign(size_t(delaySize), 0.0f);
        delayPos = 0;
        update();
        reset();
    }

    void update() {
        const float s = std::max(0.0f, std::min(1.0f, size));
        const float d = std::max(0.0f, std::min(1.0f, damp));
        combFeedback = 0.70f + s * 0.28f;                 // 0.70 .. 0.98: the tail's length
        dampCoefficient = d * 0.4f;                       // how fast the tail darkens
        // the delay's lowpass: a one-pole, dark to bright
        const float t = std::max(0.0f, std::min(1.0f, tone));
        toneCoefficient = 0.02f + t * t * 0.95f;
        // 1/32 .. 1/2 of a beat, triplets included; synced, the tempo decides and Time is left alone
        static const float beats[8] = {0.125f, 1.0f / 6.0f, 0.25f, 1.0f / 3.0f, 0.5f, 2.0f / 3.0f, 1.0f, 2.0f};
        float ms = delayMs;
        if (sync) {
            const int d = std::max(0, std::min(7, noteDivision));
            const float tempo = std::max(20.0f, std::min(300.0f, bpm));
            ms = 60000.0f / tempo * beats[d];
        }
        ms = std::max(10.0f, std::min(2000.0f, ms));  // the line is two seconds long: longer is clamped
        delaySamples = std::min(delaySize - 4, int(ms * rate / 1000.0f));
        delayFeedback = std::max(0.0f, std::min(0.95f, feedback));
    }

    void reset() {
        for (int c = 0; c < kCombs; ++c) {
            std::fill(combL[c].begin(), combL[c].end(), 0.0f);
            std::fill(combR[c].begin(), combR[c].end(), 0.0f);
            combStoreL[c] = combStoreR[c] = 0.0f;
        }
        for (int a = 0; a < kAllpasses; ++a) {
            std::fill(apL[a].begin(), apL[a].end(), 0.0f);
            std::fill(apR[a].begin(), apR[a].end(), 0.0f);
        }
        std::fill(delayL.begin(), delayL.end(), 0.0f);
        std::fill(delayR.begin(), delayR.end(), 0.0f);
        delayPos = 0;
        lowL = lowR = 0.0f;
    }

    void process(const float* inL, const float* inR, float* outL, float* outR, unsigned frames) {
        const float wet = std::max(0.0f, std::min(1.0f, mix));
        const float dry = 1.0f - wet;
        const float rev = std::max(0.0f, std::min(1.0f, reverbAmount));
        const float del = std::max(0.0f, std::min(1.0f, delayAmount));
        for (unsigned i = 0; i < frames; ++i) {
            const float l = inL[i], r = inR[i];
            // --- the delay: read the line, lowpass it into the loop, cross-feed it (ping-pong) ---
            const int read = (delayPos - delaySamples + delaySize) % delaySize;
            const float dl = delayL[size_t(read)];
            const float dr = delayR[size_t(read)];
            lowL += (dl - lowL) * toneCoefficient;  // the loop's tone
            lowR += (dr - lowR) * toneCoefficient;
            delayL[size_t(delayPos)] = 0.5f * (l + r) + lowR * delayFeedback;
            delayR[size_t(delayPos)] = 0.5f * (l + r) + lowL * delayFeedback;
            delayPos = (delayPos + 1) % delaySize;
            const float delayLOut = dl * del, delayROut = dr * del;
            // --- the reverb: the input plus the delay goes into eight combs, then four allpasses ---
            const float rl = l + delayLOut, rr = r + delayROut;
            const float sumL = combs(rl, true), sumR = combs(rr, false);
            const float revL = allpasses(sumL, true), revR = allpasses(sumR, false);
            outL[i] = dry * l + wet * (delayLOut + revL * rev);
            outR[i] = dry * r + wet * (delayROut + revR * rev);
        }
    }

private:
    // Eight parallel combs, each a delay with a damped feedback: the body of the reverb.
    float combs(float input, bool left) {
        float sum = 0.0f;
        for (int c = 0; c < kCombs; ++c) {
            std::vector<float>& line = left ? combL[c] : combR[c];
            int& pos = left ? combPosL[c] : combPosR[c];
            float& store = left ? combStoreL[c] : combStoreR[c];
            const float out = line[size_t(pos)];
            store = out * (1.0f - dampCoefficient) + store * dampCoefficient;  // the tail darkens as it dies
            line[size_t(pos)] = input * 0.015f + store * combFeedback + 1e-20f;  // the tiny offset: no denormals
            if (++pos >= int(line.size())) pos = 0;
            sum += out;
        }
        return sum;
    }

    // Four allpasses in series: they smear the combs into a diffuse tail.
    float allpasses(float input, bool left) {
        float signal = input;
        for (int a = 0; a < kAllpasses; ++a) {
            std::vector<float>& line = left ? apL[a] : apR[a];
            int& pos = left ? apPosL[a] : apPosR[a];
            const float stored = line[size_t(pos)];
            const float out = stored - signal;
            line[size_t(pos)] = signal + stored * 0.5f;
            if (++pos >= int(line.size())) pos = 0;
            signal = out;
        }
        return signal;
    }

    float rate = 48000.0f;
    float combFeedback = 0.85f, dampCoefficient = 0.16f, toneCoefficient = 0.3f, delayFeedback = 0.35f;
    float lowL = 0.0f, lowR = 0.0f;
    int delaySize = 1, delaySamples = 1, delayPos = 0;
    std::vector<float> delayL, delayR;
    std::vector<float> combL[kCombs], combR[kCombs], apL[kAllpasses], apR[kAllpasses];
    int combPosL[kCombs] = {}, combPosR[kCombs] = {}, apPosL[kAllpasses] = {}, apPosR[kAllpasses] = {};
    float combStoreL[kCombs] = {}, combStoreR[kCombs] = {};
};

#endif // SPACE_CORE_HPP_INCLUDED
