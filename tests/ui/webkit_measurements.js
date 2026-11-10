// Test-only probe; production has no rAF ticker. Each sample observes layout.
window.probe = {
  frames: [], phase: 'idle', direction: 0, highlights: [], stopped: false, wheels: 0,
  start() {
    const rows = Array.from(document.querySelectorAll('article'));
    let last;
    const frame = time => {
      if (this.stopped) return;
      let lo = 0, hi = rows.length;
      while (lo < hi) {
        const mid = (lo+hi) >>> 1;
        const rect = rows[mid].getBoundingClientRect();
        if (rect.bottom <= 0) lo = mid+1; else hi = mid;
      }
      const row = rows[Math.min(lo,rows.length-1)];
      this.frames.push({time, dt: last === undefined ? 0 : time-last,
        phase:this.phase, direction:this.direction, y:scrollY,
        upper:document.documentElement.scrollHeight,
        anchor:row ? [+row.dataset.row,-row.getBoundingClientRect().top] : [-1,0]});
      last = time;
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
    const original = cassini.highlight;
    cassini.highlight = (...args) => { const cost = original(...args); if(this.phase === 'playing') this.highlights.push(cost); return cost; };
  }
};
probe.start();
document.addEventListener('wheel', event => {
  probe.wheels++;
  if (event.deltaY) probe.direction = Math.sign(event.deltaY);
}, {passive:true});
