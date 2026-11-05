// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef DISTRHO_PLUGIN_INFO_H_INCLUDED
#define DISTRHO_PLUGIN_INFO_H_INCLUDED

#define DISTRHO_PLUGIN_BRAND   "Touch-Seq"
#define DISTRHO_PLUGIN_NAME    "Touch-Seq Acid"
#define DISTRHO_PLUGIN_URI     "urn:touch-seq:acid"
#define DISTRHO_PLUGIN_CLAP_ID "touchseq.acid"

#define DISTRHO_PLUGIN_BRAND_ID  TSeq
#define DISTRHO_PLUGIN_UNIQUE_ID TsAc

#define DISTRHO_PLUGIN_HAS_UI           0  // no UI: its knobs are Touch-Seq's HOST-4
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
#define DISTRHO_PLUGIN_CLAP_FEATURES "instrument", "synthesizer", "mono"

#endif // DISTRHO_PLUGIN_INFO_H_INCLUDED
