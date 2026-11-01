// SPDX-License-Identifier: ISC
// The DSP of Touch-Seq EQ: eight bands (bell, shelves, cuts) as RBJ biquads in series, with the parameter
// values smoothed per block (a turn does not click), plus a small analyser the app draws behind the curve.
#ifndef EQ_CORE_HPP_INCLUDED
#define EQ_CORE_HPP_INCLUDED

#include <cmath>
#include <complex>
#include <cstring>

enum class BandType { Bell = 0, LowShelf = 1, HighShelf = 2, LowCut = 3, HighCut = 4 };

struct Band {
    float freq = 1000.0f;  // Hz
    float gain = 0.0f;     // dB (cuts ignore it)
    float q = 1.4f;        // width / resonance
    int type = 0;          // BandType
};

class Biquad {
public:
    float b0 = 1.0f, b1 = 0.0f, b2 = 0.0f, a1 = 0.0f, a2 = 0.0f;  // a0 is normalised away

    void set(const Band& band, double sampleRate) {
        constexpr double pi = 3.14159265358979323846;
        const double f = band.freq < 20.0 ? 20.0 : (band.freq > sampleRate * 0.49 ? sampleRate * 0.49 : band.freq);
        const double q = band.q < 0.05 ? 0.05 : band.q;
        const double A = std::pow(10.0, band.gain / 40.0);
        const double w0 = 2.0 * pi * f / sampleRate;
        const double cw = std::cos(w0), sw = std::sin(w0);
        const double alpha = sw / (2.0 * q);
        double nb0 = 1.0, nb1 = 0.0, nb2 = 0.0, na0 = 1.0, na1 = 0.0, na2 = 0.0;
        switch (static_cast<BandType>(band.type)) {
        case BandType::LowShelf: {
            const double beta = 2.0 * std::sqrt(A) * alpha;
            nb0 = A * ((A + 1) - (A - 1) * cw + beta);
            nb1 = 2 * A * ((A - 1) - (A + 1) * cw);
            nb2 = A * ((A + 1) - (A - 1) * cw - beta);
            na0 = (A + 1) + (A - 1) * cw + beta;
            na1 = -2 * ((A - 1) + (A + 1) * cw);
            na2 = (A + 1) + (A - 1) * cw - beta;
            break;
        }
        case BandType::HighShelf: {
            const double beta = 2.0 * std::sqrt(A) * alpha;
            nb0 = A * ((A + 1) + (A - 1) * cw + beta);
            nb1 = -2 * A * ((A - 1) + (A + 1) * cw);
            nb2 = A * ((A + 1) + (A - 1) * cw - beta);
            na0 = (A + 1) - (A - 1) * cw + beta;
            na1 = 2 * ((A - 1) - (A + 1) * cw);
            na2 = (A + 1) - (A - 1) * cw - beta;
            break;
        }
        case BandType::LowCut:  // a high pass: everything below the corner goes
            nb0 = (1 + cw) / 2;
            nb1 = -(1 + cw);
            nb2 = (1 + cw) / 2;
            na0 = 1 + alpha;
            na1 = -2 * cw;
            na2 = 1 - alpha;
            break;
        case BandType::HighCut:  // a low pass
            nb0 = (1 - cw) / 2;
            nb1 = 1 - cw;
            nb2 = (1 - cw) / 2;
            na0 = 1 + alpha;
            na1 = -2 * cw;
            na2 = 1 - alpha;
            break;
        case BandType::Bell:
        default:
            nb0 = 1 + alpha * A;
            nb1 = -2 * cw;
            nb2 = 1 - alpha * A;
            na0 = 1 + alpha / A;
            na1 = -2 * cw;
            na2 = 1 - alpha / A;
            break;
        }
        b0 = float(nb0 / na0);
        b1 = float(nb1 / na0);
        b2 = float(nb2 / na0);
        a1 = float(na1 / na0);
        a2 = float(na2 / na0);
    }

    float process(float x, float& z1, float& z2) const {  // transposed direct form II
        const float y = b0 * x + z1;
        z1 = b1 * x - a1 * y + z2;
        z2 = b2 * x - a2 * y;
        return y;
    }
};

class EQCore {
public:
    static constexpr int kBandCount = 8;
    static constexpr int kSpectrumCount = 48;
    static constexpr int kAnalyserSize = 1024;

    Band bands[kBandCount];
    float spectrum[kSpectrumCount] = {0};  // 0 .. 1, -80 .. 0 dB, log-spaced bands

    EQCore() {
        resetDefaults();
        prepare(48000.0);
    }

    void resetDefaults() {
        for (int i = 0; i < kBandCount; ++i) {
            bands[i] = Band();
            bands[i].freq = 40.0f * std::pow(20000.0f / 40.0f, float(i) / float(kBandCount - 1));  // log spread
            working[i] = bands[i];
        }
        dirty = true;
    }

    void prepare(double sampleRate) {
        fs = sampleRate;
        for (int i = 0; i < kBandCount; ++i) {
            working[i] = bands[i];
            filters[i].set(working[i], fs);
        }
        std::memset(ring, 0, sizeof(ring));
        memsetStates();
        fill = blocks = 0;
    }

    void reset() { memsetStates(); }  // no state carried over: a fresh start is not a tail

    void touch() { dirty = true; }  // a parameter changed: ease towards it and follow with the coefficients

    void process(float* left, float* right, unsigned frames) {
        smooth();  // once per block
        for (unsigned n = 0; n < frames; ++n) {
            float l = left[n], r = right[n];
            for (int i = 0; i < kBandCount; ++i) {
                l = filters[i].process(l, z1L[i], z2L[i]);
                r = filters[i].process(r, z1R[i], z2R[i]);
            }
            left[n] = l;
            right[n] = r;
            pushAnalyser(0.5f * (l + r));
        }
        analyse();
    }

    /// The cascade's response at `hz`, in dB (the test uses it; the app draws the same maths).
    float magnitudeDb(float hz) const {
        constexpr double pi = 3.14159265358979323846;
        double mag = 1.0;
        for (int i = 0; i < kBandCount; ++i) {
            const Biquad& q = filters[i];
            const double w = 2.0 * pi * hz / fs;
            const std::complex<double> z1 = std::polar(1.0, -w), z2 = z1 * z1;
            const std::complex<double> num = double(q.b0) + double(q.b1) * z1 + double(q.b2) * z2;
            const std::complex<double> den = 1.0 + double(q.a1) * z1 + double(q.a2) * z2;
            mag *= std::abs(num / den);
        }
        return float(20.0 * std::log10(mag <= 1e-9 ? 1e-9 : mag));
    }

    /// Drop the easing: the next block is at the target (the plugin does this when it is prepared).
    void settle() {
        for (int i = 0; i < kBandCount; ++i) {
            working[i] = bands[i];
            filters[i].set(working[i], fs);
        }
        dirty = false;
    }

private:
    Band working[kBandCount];
    Biquad filters[kBandCount];
    float z1L[kBandCount] = {0}, z2L[kBandCount] = {0}, z1R[kBandCount] = {0}, z2R[kBandCount] = {0};
    double fs = 48000.0;
    bool dirty = true;

    void memsetStates() {
        for (int i = 0; i < kBandCount; ++i) z1L[i] = z2L[i] = z1R[i] = z2R[i] = 0.0f;
    }

    void smooth() {
        if (not dirty) return;
        dirty = false;
        for (int i = 0; i < kBandCount; ++i) {
            Band& w = working[i];
            const Band& t = bands[i];
            const bool changed = std::fabs(w.freq - t.freq) > 0.5f or std::fabs(w.gain - t.gain) > 0.01f
                                 or std::fabs(w.q - t.q) > 0.001f or w.type != t.type;
            if (not changed) continue;
            w.type = t.type;  // a type is not a number to ease
            w.freq += (t.freq - w.freq) * 0.25f;
            w.gain += (t.gain - w.gain) * 0.25f;
            w.q += (t.q - w.q) * 0.25f;
            if (std::fabs(w.freq - t.freq) < 1.0f) w.freq = t.freq;
            if (std::fabs(w.gain - t.gain) < 0.02f) w.gain = t.gain;
            if (std::fabs(w.q - t.q) < 0.005f) w.q = t.q;
            filters[i].set(w, fs);
            dirty = true;  // still easing: come back next block
        }
    }

    // --- analyser -----------------------------------------------------------
    float ring[kAnalyserSize] = {0};
    float re[kAnalyserSize] = {0}, im[kAnalyserSize] = {0};
    unsigned fill = 0, blocks = 0;

    void pushAnalyser(float x) {
        ring[fill++] = x;
        if (fill >= kAnalyserSize) fill = 0;
    }

    void analyse() {
        if (++blocks < 3) return;  // every third block: smooth enough for the app's 25 Hz
        blocks = 0;
        constexpr double pi = 3.14159265358979323846;
        for (int i = 0; i < kAnalyserSize; ++i) {
            const float w = 0.5f - 0.5f * std::cos(2.0f * float(pi) * float(i) / float(kAnalyserSize));
            re[i] = ring[(fill + i) % kAnalyserSize] * w;
            im[i] = 0.0f;
        }
        fft();
        const double binHz = fs / double(kAnalyserSize);
        for (int b = 0; b < kSpectrumCount; ++b) {
            const double lo = 20.0 * std::pow(20000.0 / 20.0, double(b) / kSpectrumCount);
            const double hi = 20.0 * std::pow(20000.0 / 20.0, double(b + 1) / kSpectrumCount);
            int first = int(lo / binHz), last = int(hi / binHz) + 1;
            if (first < 1) first = 1;
            if (last <= first) last = first + 1;
            if (last > kAnalyserSize / 2 - 1) last = kAnalyserSize / 2 - 1;
            float peak = 0.0f;
            for (int k = first; k < last; ++k) {
                const float mag = std::sqrt(re[k] * re[k] + im[k] * im[k]) * (2.0f / kAnalyserSize);
                if (mag > peak) peak = mag;
            }
            const float db = peak <= 1e-6f ? -80.0f : 20.0f * std::log10(peak);
            const float target = (db + 80.0f) / 80.0f;
            spectrum[b] += (target - spectrum[b]) * 0.35f;  // jump up, ease down
            if (spectrum[b] < 0.0f) spectrum[b] = 0.0f;
        }
    }

    void fft() {  // in place, radix 2
        const int n = kAnalyserSize;
        for (int i = 1, j = 0; i < n; ++i) {
            int bit = n >> 1;
            for (; j & bit; bit >>= 1) j ^= bit;
            j ^= bit;
            if (i < j) {
                const float t = re[i]; re[i] = re[j]; re[j] = t;
                const float u = im[i]; im[i] = im[j]; im[j] = u;
            }
        }
        for (int len = 2; len <= n; len <<= 1) {
            const double angle = -2.0 * 3.14159265358979323846 / len;
            const double wr = std::cos(angle), wi = std::sin(angle);
            for (int i = 0; i < n; i += len) {
                double cr = 1.0, ci = 0.0;
                for (int k = 0; k < len / 2; ++k) {
                    const int a = i + k, b = a + len / 2;
                    const double yr = re[b] * cr - im[b] * ci;
                    const double yi = re[b] * ci + im[b] * cr;
                    const double xr = re[a], xi = im[a];
                    re[a] = float(xr + yr); im[a] = float(xi + yi);
                    re[b] = float(xr - yr); im[b] = float(xi - yi);
                    const double ncr = cr * wr - ci * wi;
                    ci = cr * wi + ci * wr;
                    cr = ncr;
                }
            }
        }
    }
};

#endif // EQ_CORE_HPP_INCLUDED
