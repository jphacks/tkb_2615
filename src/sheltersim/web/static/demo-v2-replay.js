/* Bounded one-minute read buffer and one shared wall-time replay clock. */
(() => {
  "use strict";
  const frames = (p) => p.frames || [p];
  class Player {
    constructor(options) {
      this.options = options;
      this.clock = new window.HinanjoReplayClock(options.minute, options.rate);
      this.buffer = new Map([[options.minute, options.payload]]);
      this.head = options.minute;
      this.hi = options.hi;
      this.lastMinute = options.minute;
      this.visualMs = 0;
      this.motion = null;
      this.gaits = new Map();
      this.loading = false;
      this.version = 0;
      this.raf = null;
      this.disposed = false;
    }
    play() {
      if (this.disposed || this.clock.running) return;
      this.clock.start(performance.now());
      this.version++;
      this.stats = { frames: 0, drawMs: 0, start: performance.now() };
      this.options.onState("playing");
      this.fill(this.version);
      this.loop(this.version);
    }
    pause() {
      this.clock.running = false;
      this.clock.wall = performance.now();
      this.version++;
      cancelAnimationFrame(this.raf);
      this.raf = null;
      this.options.onState("paused");
    }
    dispose() {
      this.pause();
      this.disposed = true;
      this.buffer.clear();
    }
    setRate(rate) {
      this.clock.setRate(rate, performance.now());
    }
    async fill(version) {
      if (this.loading || !this.clock.running || this.disposed) return;
      this.loading = true;
      try {
        while (
          this.clock.running &&
          version === this.version &&
          this.head < this.hi &&
          this.head < Math.floor(this.clock.position) + 8
        ) {
          const minute = this.head + 1,
            end = this.options.fetchRange
              ? Math.min(this.hi, minute + 7)
              : minute;
          const payloads = this.options.fetchRange
            ? await this.options.fetchRange(minute, end)
            : [await this.options.fetch(minute)];
          if (this.disposed || version !== this.version) return;
          if (
            payloads.length !== end - minute + 1 ||
            payloads.some((payload, i) =>
              frames(payload).some(
                (f) => f.minute !== minute + i || f.computed_until < minute + i,
              ),
            )
          )
            throw new Error("同じ計算済み時刻の記録を取得できませんでした。");
          payloads.forEach((payload, i) =>
            this.buffer.set(minute + i, payload),
          );
          this.head = end;
        }
      } catch (e) {
        if (version === this.version) {
          this.pause();
          this.options.onError(e);
        }
      } finally {
        this.loading = false;
      }
    }
    loop(version) {
      if (!this.clock.running || version !== this.version || this.disposed)
        return;
      const now = performance.now(),
        elapsed = Math.max(0, now - this.clock.wall);
      this.visualMs += elapsed;
      this.clock.available = this.head;
      const position = this.clock.tick(now),
        minute = Math.min(this.hi, Math.floor(position + 1e-9));
      for (let m = this.lastMinute; m < minute; m++) {
        const a = this.buffer.get(m),
          b = this.buffer.get(m + 1);
        if (!a || !b) break;
        frames(a).forEach((f, i) => {
          const plan = window.HinanjoScene.prepare(
            f,
            frames(b)[i],
            this.options.mode(),
          );
          if (!this.gaits.has(f.branch_id))
            this.gaits.set(f.branch_id, new Map());
          const offsets = this.gaits.get(f.branch_id);
          for (const e of [...plan.entries, ...(plan.staffEntries || [])])
            if (e.path)
              offsets.set(
                e.shown.agent_id || e.shown.id,
                (offsets.get(e.shown.agent_id || e.shown.id) || 0) + e.length,
              );
        });
      }
      if (minute !== this.lastMinute) {
        this.options.onFrame(this.buffer.get(minute), minute);
        this.lastMinute = minute;
        for (const key of this.buffer.keys())
          if (key < minute - 1) this.buffer.delete(key);
      }
      const current = this.buffer.get(minute),
        next = this.buffer.get(minute + 1);
      this.motion = next
        ? {
            from: frames(current),
            to: frames(next),
            fraction: position - minute,
          }
        : null;
      this.options.onDraw(this);
      this.stats.frames++;
      this.stats.drawMs += performance.now() - now;
      if (now - this.stats.start >= 1000) {
        this.options.onStats({
          fps: (this.stats.frames * 1000) / (now - this.stats.start),
          drawMs: this.stats.drawMs / this.stats.frames,
          minute: position,
          rate: this.clock.rate,
          bufferedUntil: this.head,
          endpoints: this.motion
            ? {
                from: this.motion.from.map((f) => f.minute),
                to: this.motion.to.map((f) => f.minute),
              }
            : null,
          fraction: this.motion?.fraction || 0,
          displayMode: this.options.mode(),
        });
        this.stats = { frames: 0, drawMs: 0, start: now };
      }
      if (position >= this.hi) {
        this.pause();
        this.options.onState("end");
        return;
      }
      this.fill(version);
      this.raf = requestAnimationFrame(() => this.loop(version));
    }
  }
  window.HinanjoReplayPlayer = Player;
})();
