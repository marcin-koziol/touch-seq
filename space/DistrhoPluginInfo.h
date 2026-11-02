// SPDX-License-Identifier: ISC
#ifndef DISTRHO_PLUGIN_INFO_H_INCLUDED
#define DISTRHO_PLUGIN_INFO_H_INCLUDED

#define DISTRHO_PLUGIN_BRAND   "Touch-Seq"
#define DISTRHO_PLUGIN_NAME    "Touch-Seq Space"
#define DISTRHO_PLUGIN_URI     "urn:touch-seq:space"
#define DISTRHO_PLUGIN_CLAP_ID "touchseq.space"

#define DISTRHO_PLUGIN_BRAND_ID  TSeq
#define DISTRHO_PLUGIN_UNIQUE_ID TsSp

#define DISTRHO_PLUGIN_HAS_UI           0  // no UI: its knobs are Touch-Seq's
#define DISTRHO_PLUGIN_IS_RT_SAFE       1
#define DISTRHO_PLUGIN_NUM_INPUTS       2
#define DISTRHO_PLUGIN_NUM_OUTPUTS      2
#define DISTRHO_PLUGIN_WANT_STATE       0
#define DISTRHO_PLUGIN_WANT_PROGRAMS    0
#define DISTRHO_PLUGIN_WANT_TIMEPOS     0

#define DISTRHO_PLUGIN_LV2_CATEGORY  "lv2:ReverbPlugin"
#define DISTRHO_PLUGIN_CLAP_FEATURES "audio-effect", "stereo", "reverb", "delay"

#endif // DISTRHO_PLUGIN_INFO_H_INCLUDED
