// SPDX-License-Identifier: ISC
#ifndef DISTRHO_PLUGIN_INFO_H_INCLUDED
#define DISTRHO_PLUGIN_INFO_H_INCLUDED

#define DISTRHO_PLUGIN_BRAND   "Touch-Seq"
#define DISTRHO_PLUGIN_NAME    "Touch-Seq XYFX"
#define DISTRHO_PLUGIN_URI     "urn:touch-seq:xyfx"
#define DISTRHO_PLUGIN_CLAP_ID "touchseq.xyfx"

#define DISTRHO_PLUGIN_BRAND_ID  TSeq
#define DISTRHO_PLUGIN_UNIQUE_ID XyFx

#define DISTRHO_PLUGIN_HAS_UI           0  // the UI is Touch-Seq's XY pad
#define DISTRHO_PLUGIN_IS_RT_SAFE       1
#define DISTRHO_PLUGIN_NUM_INPUTS       2
#define DISTRHO_PLUGIN_NUM_OUTPUTS      2
#define DISTRHO_PLUGIN_WANT_STATE       0
#define DISTRHO_PLUGIN_WANT_PROGRAMS    0
#define DISTRHO_PLUGIN_WANT_TIMEPOS     0  // Touch-Seq sets BPM as a parameter (its Carla runs without transport)

#define DISTRHO_PLUGIN_LV2_CATEGORY  "lv2:DelayPlugin"
#define DISTRHO_PLUGIN_CLAP_FEATURES "audio-effect", "stereo", "glitch", "filter"

#endif // DISTRHO_PLUGIN_INFO_H_INCLUDED
