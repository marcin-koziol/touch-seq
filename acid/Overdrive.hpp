// SPDX-License-Identifier: GPL-3.0-or-later
// The jc303 overdrive: GuitarML BYOD's neural LSTM model (Ibanez TS808 Tube Screamer, one of jc303's BYOD
// models), with a fixed -12 dB input drive, half dry and a little makeup on the wet side. RTNeural (BSD-3)
// runs the model; the model JSON is embedded in overdrive_model.hpp. See overdrive/LICENSE.
#ifndef ACID_OVERDRIVE_HPP_INCLUDED
#define ACID_OVERDRIVE_HPP_INCLUDED

#ifndef RTNEURAL_USE_EIGEN
#define RTNEURAL_USE_EIGEN 0
#endif
#ifndef RTNEURAL_USE_XSIMD
#define RTNEURAL_USE_XSIMD 0
#endif
#ifndef RTNEURAL_DEFAULT_ALIGNMENT
#define RTNEURAL_DEFAULT_ALIGNMENT 16
#endif

#include <algorithm>
#include <cmath>

#include "RTNeural/RTNeural.h"
#include "overdrive_model.hpp"

class NeuralOverdrive {
public:
    using Model = RTNeural::ModelT<float, 1, 1,
        RTNeural::LSTMLayerT<float, 1, 40, RTNeural::SampleRateCorrectionMode::LinInterp>,
        RTNeural::DenseT<float, 40, 1>>;

    void prepare(double sampleRate) {
        rate = sampleRate > 1000.0 ? sampleRate : 48000.0;
        if (!loaded) {
            const auto json = nlohmann::json::parse(kOverdriveModelJson);
            const auto& state = json.at("state_dict");
            RTNeural::torch_helpers::loadLSTM<float>(state, "rec.", model.get<0>());
            RTNeural::torch_helpers::loadDense<float>(state, "lin.", model.get<1>());
            loaded = true;
        }
        // the model was trained at 44.1 kHz; LinInterp stretches the recurrent delay to our rate
        const float delay = std::max(1.0f, float(rate / 44100.0));
        model.get<0>().prepare(delay);
        reset();
    }

    void reset() {
        model.reset();
        dcX = dcY = 0.0f;
    }

    float process(float x) {
        const float driven = x * kInputGain;  // jc303: overdriveLevel (0.15 dB) - 12 dB
        const float wet = model.forward(&driven);
        const float mixed = kDryGain * x + kWetGain * wet;
        const float out = mixed - dcX + 0.995f * dcY;  // the DC blocker after the model
        dcX = mixed;
        dcY = out;
        return out;
    }

private:
    static constexpr float kInputGain = 0.255859f;  // 10^(-11.85/20), the TS808's input trim
    static constexpr float kDryGain = 0.707107f;    // half dry (sin3dB at 50%)
    static constexpr float kWetGain = 1.600000f;    // the model output is quiet: makeup for the switch

    Model model;
    bool loaded = false;
    double rate = 48000.0;
    float dcX = 0.0f, dcY = 0.0f;
};

#endif // ACID_OVERDRIVE_HPP_INCLUDED
