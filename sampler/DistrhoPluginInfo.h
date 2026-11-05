// SPDX-License-Identifier: ISC
#ifndef DISTRHO_PLUGIN_INFO_H_INCLUDED
#define DISTRHO_PLUGIN_INFO_H_INCLUDED

#define DISTRHO_PLUGIN_BRAND   "Touch-Seq"
#define DISTRHO_PLUGIN_NAME    "Touch-Seq Sampler"
#define DISTRHO_PLUGIN_URI     "urn:touch-seq:sampler"
#define DISTRHO_PLUGIN_CLAP_ID "touchseq.sampler"

#define DISTRHO_PLUGIN_BRAND_ID  TSeq
#define DISTRHO_PLUGIN_UNIQUE_ID TsSm

#define DISTRHO_PLUGIN_HAS_UI           0  // no UI: Touch-Seq's sampler editor and pads are its face
#define DISTRHO_PLUGIN_IS_RT_SAFE       1
#define DISTRHO_PLUGIN_IS_SYNTH         1
#define DISTRHO_PLUGIN_NUM_INPUTS       0
#define DISTRHO_PLUGIN_NUM_OUTPUTS      2
#define DISTRHO_PLUGIN_WANT_MIDI_INPUT  1
#define DISTRHO_PLUGIN_WANT_MIDI_OUTPUT 0
#define DISTRHO_PLUGIN_WANT_STATE       0
#define DISTRHO_PLUGIN_WANT_PROGRAMS    0
#define DISTRHO_PLUGIN_WANT_TIMEPOS     0

#define DISTRHO_PLUGIN_LV2_CATEGORY  "lv2:InstrumentPlugin"
#define DISTRHO_PLUGIN_CLAP_FEATURES "instrument", "synthesizer", "sampler"

#endif // DISTRHO_PLUGIN_INFO_H_INCLUDED
