// SPDX-License-Identifier: ISC
// DSP checks for XYFXCore: dry when idle, repeat length per division, pad layout, low-pass at the edge, release.
#include <cassert>
#include <cmath>
#include <cstdio>
#include <string>
#include <vector>

#include "XYFXCore.hpp"

static double rms(const std::vector<float>& v, size_t from, size_t to)
{
    double s = 0;
    for (size_t i = from; i < to; ++i)
        s += v[i] * v[i];
    return std::sqrt(s / (to - from));
}

static void run(XYFXCore& fx, const std::vector<float>& in, std::vector<float>& out, size_t from, size_t to)
{
    for (size_t i = from; i < to; i += 256) {  // host-sized blocks
        const unsigned n = static_cast<unsigned>(std::min<size_t>(256, to - i));
        fx.process(&in[i], &in[i], &out[i], &out[i] + 0, n);
    }
}

int main()
{
    const double sr = 48000;
    XYFXCore fx;
    fx.prepare(sr);
    fx.bpm = 120;
    const size_t n = sr * 2;
    std::vector<float> in(n), out(n), right(n);
    for (size_t i = 0; i < n; ++i)  // a non-repeating signal: rising chirp
        in[i] = std::sin(2 * M_PI * (200 + 400.0 * i / n) * i / sr) * 0.5f;

    // idle: bit-exact dry
    run(fx, in, out, 0, 4800);
    for (size_t i = 0; i < 4800; ++i)
        assert(out[i] == in[i]);

    // engage at 1/16: the pattern repeats every 6000 samples (0.125 s at 120 BPM)
    fx.x = 0.95f;  // the rightmost column: 1/16
    fx.y = 0.5f;   // the centre line: filter open
    fx.engage = 1.0f;
    const size_t len = fx.repeatLength(), start = 4800;
    std::printf("1/16 at 120 BPM: %zu samples\n", len);
    assert(len == 6000);
    run(fx, in, out, start, start + 5 * len);
    double diff = 0, level = 0;
    for (size_t i = start + 2 * len + 200; i < start + 3 * len - 200; ++i) {  // skip edge fades
        diff = std::max(diff, double(std::fabs(out[i] - out[i + len])));
        level = std::max(level, double(std::fabs(out[i])));
    }
    std::printf("repeat: level %.3f, max difference between repeats %.6f\n", level, diff);
    assert(level > 0.3 && diff < 1e-4);
    // and it is not the live input any more
    double live = 0;
    for (size_t i = start + 3 * len + 200; i < start + 4 * len - 200; ++i)
        live = std::max(live, double(std::fabs(out[i] - in[i])));
    assert(live > 0.1);

    // held far longer than the capture buffer (4.5 s): still repeating
    {
        XYFXCore held;
        held.prepare(sr);
        held.bpm = 120;
        held.x = 0.9f;
        held.engage = 1.0f;
        const size_t longN = sr * 12;
        std::vector<float> lin(longN), lout(longN);
        for (size_t i = 0; i < longN; ++i)
            lin[i] = std::sin(2 * M_PI * 440 * i / sr) * 0.5f;
        run(held, lin, lout, 0, longN);
        const double late = rms(lout, longN - sr, longN);
        std::printf("held 12 s: level in the last second %.3f\n", late);
        assert(late > 0.2);
    }

    // pad layout: right half straight 1/16 at the edge .. 1/2 at the centre, left half triplets (mode 0)
    {
        XYFXCore l;
        l.prepare(sr);
        const double beatsAt[8] = {0.25 * 2 / 3, 0.5 * 2 / 3, 2.0 / 3, 4.0 / 3, 2.0, 1.0, 0.5, 0.25};
        for (int col = 0; col < 8; ++col) {
            l.x = (col + 0.5f) / 8;
            assert(l.repeatLength() == size_t(beatsAt[col] * 0.5 * sr));  // 120 BPM: 0.5 s a beat
        }
        l.mode = XYFXCore::kReverse;  // reverse keeps straight lengths
        l.x = 0.05f;
        assert(l.repeatLength() == size_t(0.25 * 0.5 * sr) && l.reversed());
    }
    assert(XYFXCore::cutoffFor(0.5f) == 20000.0f && XYFXCore::cutoffFor(0.0f) < 81);  // LPF below
    assert(XYFXCore::cutoffFor(0.51f) < 25 && XYFXCore::cutoffFor(1.0f) > 5900);         // HPF above
    std::printf("layout: triplets | straight, LPF below the centre line, HPF above\n");

    // reverse: after the captured pass, the slice plays backwards
    {
        XYFXCore r;
        r.prepare(sr);
        r.mode = XYFXCore::kReverse;
        r.x = 0.05f;  // left edge: 1/16 backwards
        r.engage = 1.0f;
        const size_t rl = r.repeatLength();
        std::vector<float> ramp(rl * 4), rout(rl * 4);
        for (size_t i = 0; i < ramp.size(); ++i)
            ramp[i] = float(i % rl) / rl - 0.5f;  // a rising ramp per slice
        run(r, ramp, rout, 0, ramp.size());
        // second pass, away from the edge fades: falling where the input rises
        assert(rout[2 * rl + rl / 4] > rout[2 * rl + rl / 2] && rout[2 * rl + rl / 2] > rout[2 * rl + 3 * rl / 4]);
        std::printf("reverse: slice plays backwards\n");
    }

    // high-pass at the top: a 100 Hz tone mostly gone
    {
        XYFXCore hp;
        hp.prepare(sr);
        std::vector<float> lowTone(n), hout(n);
        for (size_t i = 0; i < n; ++i)
            lowTone[i] = std::sin(2 * M_PI * 100 * i / sr) * 0.5f;
        hp.x = 0.55f;
        hp.y = 1.0f;
        hp.engage = 1.0f;
        run(hp, lowTone, hout, 0, sr);
        const double hr = rms(hout, sr / 2, sr) / rms(lowTone, sr / 2, sr);
        std::printf("100 Hz through HPF at Y=1: %.4f of the level\n", hr);
        assert(hr < 0.01);
    }

    // division follows X: 1/2 note = 1 s (next to the centre)
    fx.x = 0.55f;
    std::printf("1/2 at 120 BPM: %zu samples\n", fx.repeatLength());
    assert(fx.repeatLength() == 48000);

    // low-pass at the bottom of Y: a 5 kHz tone mostly gone
    XYFXCore lp;
    lp.prepare(sr);
    std::vector<float> tone(n), lout(n);
    for (size_t i = 0; i < n; ++i)
        tone[i] = std::sin(2 * M_PI * 5000 * i / sr) * 0.5f;
    lp.x = 0.55f;  // long repeat: the first second is the live input, filtered
    lp.y = 0.0f;   // bottom edge: filter closed
    lp.engage = 1.0f;
    run(lp, tone, lout, 0, sr);
    const double ratio = rms(lout, sr / 2, sr) / rms(tone, sr / 2, sr);
    std::printf("5 kHz through LPF at Y=0: %.4f of the level\n", ratio);
    assert(ratio < 0.01);

    // release: back to dry within ~10 ms
    lp.engage = 0.0f;
    run(lp, tone, lout, sr, sr + sr / 10);
    for (size_t i = sr + sr / 50; i < sr + sr / 10; ++i)
        assert(lout[i] == tone[i]);
    std::printf("release: dry again\nOK\n");
    return 0;
}
