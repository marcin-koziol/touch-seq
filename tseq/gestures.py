"""One gesture per touch point: the generic ones (a vertical drag, a hold, a scroll-or-tap) that any panel
can hand out from a hit region. Panel-specific gestures live next to their panel."""
import time


class Gesture:
    """Interaction owned by one touch point, created by a hit region on press."""

    def move(self, pos):
        pass

    def release(self, pos):
        pass

    def cancel(self):
        """The touch was taken away (TouchCancel): undo any 'held' state, but don't act."""

    def tick(self):
        pass


class Drag(Gesture):
    """Vertical drag adjusts a value; a release without movement counts as a tap."""
    THRESHOLD = 8

    HOLD = 0.6  # s without moving: on_hold

    def __init__(self, pos, get, set_, lo, hi, px, unit=1, on_tap=None, on_hold=None):
        self.y0, self.v0 = pos.y(), get()
        self.set, self.lo, self.hi, self.px, self.unit, self.on_tap = set_, lo, hi, px, unit, on_tap
        self.on_hold, self.t0, self.held = on_hold, time.monotonic(), False
        self.dragging = False

    def tick(self):
        if self.on_hold and not self.dragging and not self.held and time.monotonic() - self.t0 >= self.HOLD:
            self.held = True
            self.on_hold()

    def move(self, pos):
        if self.held:
            return
        dy = self.y0 - pos.y()
        if not self.dragging:
            if abs(dy) >= self.THRESHOLD:
                self.dragging, self.y0 = True, pos.y()
            return
        v = self.v0 + round(dy / self.px) * self.unit
        self.set(round(max(self.lo, min(self.hi, v)), 2))

    def release(self, pos):
        if not self.dragging and not self.held and self.on_tap:
            self.on_tap()


class Hold(Gesture):
    """Fires after being held for `secs` (for destructive actions)."""

    def __init__(self, fn, secs=0.8, key=None):
        self.fn, self.secs, self.key, self.t0, self.fired = fn, secs, key, time.monotonic(), False

    def progress(self):
        return min(1.0, (time.monotonic() - self.t0) / self.secs)

    def tick(self):
        if not self.fired and self.progress() >= 1:
            self.fired = True
            self.fn()


class ScrollOrTap(Gesture):
    """Vertical drag scrolls (value moves by -dy/px); a release without movement counts as a tap."""

    def __init__(self, pos, get, set_, on_tap, px=1.0):
        self.y0, self.v0, self.set, self.on_tap, self.px, self.moved = pos.y(), get(), set_, on_tap, px, False

    def move(self, pos):
        dy = pos.y() - self.y0
        self.moved = self.moved or abs(dy) > Drag.THRESHOLD
        if self.moved:
            self.set(self.v0 - dy / self.px)

    def release(self, pos):
        if not self.moved:
            self.on_tap()


class Slide(Gesture):
    """Vertical slider: the finger's height inside `track` is the value (0..1)."""

    def __init__(self, track, set_, pos):
        self.track, self.set = track, set_
        self.move(pos)

    def move(self, pos):
        self.set(max(0.0, min(1.0, (self.track.bottom() - pos.y()) / self.track.height())))
