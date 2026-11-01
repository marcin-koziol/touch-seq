// SPDX-License-Identifier: ISC
#ifndef DISTRHO_PLUGIN_INFO_H_INCLUDED
#define DISTRHO_PLUGIN_INFO_H_INCLUDED

#define DISTRHO_PLUGIN_BRAND   "Touch-Seq"
#define DISTRHO_PLUGIN_NAME    "Touch-Seq EQ"
#define DISTRHO_PLUGIN_URI     "urn:touch-seq:eq"
#define DISTRHO_PLUGIN_CLAP_ID "touchseq.eq"

#define DISTRHO_PLUGIN_BRAND_ID  TSeq
#define DISTRHO_PLUGIN_UNIQUE_ID TsEq

#define DISTRHO_PLUGIN_HAS_UI           0  // the curve is drawn by Touch-Seq, in the host panel
#define DISTRHO_PLUGIN_IS_RT_SAFE       1
#define DISTRHO_PLUGIN_NUM_INPUTS       2
#define DISTRHO_PLUGIN_NUM_OUTPUTS      2
#define DISTRHO_PLUGIN_WANT_STATE       0
#define DISTRHO_PLUGIN_WANT_PROGRAMS    0
#define DISTRHO_PLUGIN_WANT_TIMEPOS     0

#define DISTRHO_PLUGIN_LV2_CATEGORY  "lv2:EQPlugin"
#define DISTRHO_PLUGIN_CLAP_FEATURES "audio-effect", "stereo", "equalizer", "eq"

#endif // DISTRHO_PLUGIN_INFO_H_INCLUDED
