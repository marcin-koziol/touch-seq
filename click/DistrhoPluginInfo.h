// SPDX-License-Identifier: ISC
#ifndef DISTRHO_PLUGIN_INFO_H_INCLUDED
#define DISTRHO_PLUGIN_INFO_H_INCLUDED

#define DISTRHO_PLUGIN_BRAND   "Touch-Seq"
#define DISTRHO_PLUGIN_NAME    "Touch-Seq Click"
#define DISTRHO_PLUGIN_URI     "urn:touch-seq:click"
#define DISTRHO_PLUGIN_CLAP_ID "touchseq.click"

#define DISTRHO_PLUGIN_BRAND_ID  TSeq
#define DISTRHO_PLUGIN_UNIQUE_ID Clk_

#define DISTRHO_PLUGIN_HAS_UI           0  // no UI: one level knob, in HOST-4 if you want it
#define DISTRHO_PLUGIN_IS_RT_SAFE       1
#define DISTRHO_PLUGIN_NUM_INPUTS       2
#define DISTRHO_PLUGIN_NUM_OUTPUTS      2
#define DISTRHO_PLUGIN_WANT_MIDI_INPUT  1  // a note per beat: that is the metronome
#define DISTRHO_PLUGIN_WANT_STATE       0
#define DISTRHO_PLUGIN_WANT_PROGRAMS    0
#define DISTRHO_PLUGIN_WANT_TIMEPOS     0

#define DISTRHO_PLUGIN_LV2_CATEGORY  "lv2:UtilityPlugin"
#define DISTRHO_PLUGIN_CLAP_FEATURES "audio-effect", "stereo", "utility", "metronome"

#endif // DISTRHO_PLUGIN_INFO_H_INCLUDED
