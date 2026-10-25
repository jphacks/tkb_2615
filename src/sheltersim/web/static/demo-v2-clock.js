/* One bounded replay clock, measured in simulation minutes. 60x = 1 minute/second. */
(() => {
  "use strict";
  class ReplayClock {
    constructor(position = 0, rate = 60) {
      this.position = position;
      this.rate = rate;
      this.wall = null;
      this.running = false;
      this.available = position;
    }
    tick(now) {
      if (this.running && this.wall !== null) {
        this.position = Math.min(
          this.available,
          this.position + (Math.max(0, now - this.wall) * this.rate) / 60000,
        );
      }
      this.wall = now;
      return this.position;
    }
    start(now) {
      this.wall = now;
      this.running = true;
    }
    pause(now) {
      this.tick(now);
      this.running = false;
    }
    setRate(rate, now) {
      this.tick(now);
      this.rate = rate;
    }
    seek(position, now) {
      this.position = position;
      this.wall = now;
      this.available = position;
    }
  }
  window.HinanjoReplayClock = ReplayClock;
})();
