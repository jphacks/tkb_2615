/* Opt-in requestAnimationFrame redraw samples; not a benchmark or device profile. */
window.ShelterSimFrameMeter = class {
  constructor(limit = 600) { this.limit = limit; this.samples = []; this.previous = null; this.handle = null; this.active = false; }
  frame(now = performance.now()) {
    if (document.visibilityState !== "visible") { this.previous = null; return; }
    if (this.previous !== null) {
      const elapsed = now - this.previous;
      if (Number.isFinite(elapsed) && elapsed > 0) {
        this.samples.push(elapsed);
        if (this.samples.length > this.limit) this.samples.shift();
      }
    }
    this.previous = now;
  }
  start(redraw) {
    this.stop(); this.active = true; this.previous = null;
    const tick = now => {
      if (!this.active) return;
      this.frame(now);
      if (document.visibilityState === "visible") redraw();
      this.handle = requestAnimationFrame(tick);
    };
    this.handle = requestAnimationFrame(tick);
  }
  stop() {
    this.active = false;
    if (this.handle !== null) cancelAnimationFrame(this.handle);
    this.handle = null; this.previous = null;
  }
  reset() { this.previous = null; this.samples = []; }
  report() {
    const values = [...this.samples].sort((a, b) => a - b);
    const mean = values.reduce((a, b) => a + b, 0) / Math.max(1, values.length);
    return { samples: values.length, fps: mean ? 1000 / mean : null,
      p95_frame_ms: values.length ? values[Math.floor((values.length - 1) * 0.95)] : null,
      visibility: document.visibilityState, user_agent: navigator.userAgent };
  }
};
