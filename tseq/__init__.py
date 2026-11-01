"""Touch-Seq's insides, one module per concern:

    constants   sizes, colours, tables (no imports but os)
    music       scales, chords, XYFX mappings (no Qt)
    model       the persisted project and its files
    engine      MIDI out and the clock/playback thread
    gestures    the generic touch gestures (drag, hold, scroll-or-tap)
    ui          the drawing toolkit, `Panel`, and the module `Window`
    sequencer   the step sequencer module (and its gestures)
    controls    the fader bank module
    host        the plugin host (Carla) module
    performer   the live-play module (pads, keyboard, XY pad)
    patchbay    the audio patchbay (two columns of ports, tap to link)
    eq          the Touch-Seq EQ's editor (the curve, the bands, the spectrum)
    rack        the four modules composed into one window
    dialogs     project chooser and settings
    app         config, startup, project switching

Dependencies run that way; nothing below imports anything above it. The `touchseq.py` script beside this
package is the entry point and re-exports everything here, so `import touchseq as ts` still works.
"""
