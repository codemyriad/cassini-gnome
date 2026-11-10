// SPDX-License-Identifier: GPL-3.0-or-later
// No animation loop in normal operation. The bridge mutates only changed words.
"use strict";
(() => {
  const post = message => window.webkit.messageHandlers.cassini.postMessage(JSON.stringify(message));
  let words, starts, ends, current = new Set(), lastPosition = -1;
  function index() {
    words = Array.from(document.querySelectorAll('.cassini-word'));
    for (const word of words) word.classList.remove('current');
    starts = words.slice().sort((a,b) => +a.dataset.start - +b.dataset.start);
    ends = words.slice().sort((a,b) => +a.dataset.end - +b.dataset.end);
    current.clear(); lastPosition = -1;
  }
  function upperBound(array, time, field) {
    let a = 0, b = array.length;
    while (a < b) { const m = (a+b) >>> 1; if (+array[m].dataset[field] <= time) a = m+1; else b = m; }
    return a;
  }
  function highlight(position, follow = false) {
    const before = performance.now();
    let next;
    if (lastPosition >= 0 && position >= lastPosition && position-lastPosition < 2000) {
      next = new Set(current);
      for (let i = upperBound(starts,lastPosition,'start'); i < starts.length && +starts[i].dataset.start <= position; i++) next.add(starts[i]);
      for (const word of next) if (+word.dataset.end <= position) next.delete(word);
    } else {
      // Random seek: choose the smaller temporal prefix/suffix, then intersect.
      const a = upperBound(starts,position,'start'), b = upperBound(ends,position,'end');
      next = new Set((a < ends.length-b ? starts.slice(0,a) : ends.slice(b)).filter(w => +w.dataset.start <= position && +w.dataset.end > position));
    }
    for (const word of current) if (!next.has(word)) word.classList.remove('current');
    for (const word of next) if (!current.has(word)) word.classList.add('current');
    current = next; lastPosition = position;
    if (follow && current.size) {
      const word = current.values().next().value, rect = word.getBoundingClientRect();
      if (rect.top < 0 || rect.bottom > innerHeight) word.scrollIntoView({block:'nearest',behavior:'instant'});
    }
    return performance.now()-before;
  }
  window.cassini = {
    highlight,
    follow(row) { document.getElementById('r'+row)?.scrollIntoView({block:'nearest',behavior:'instant'}); },
    listening(rows) {
      document.querySelectorAll('.listen-current').forEach(n => n.classList.remove('listen-current'));
      for (const row of rows) document.getElementById('r'+row)?.classList.add('listen-current');
    },
    theme(dark) { document.body.classList.toggle('dark',dark); },
    replaceTail(first, html) {
      const main = document.querySelector('main');
      Array.from(main.children).filter(n => +n.dataset.row >= first).forEach(n => n.remove());
      main.insertAdjacentHTML('beforeend',html); index();
    },
    // Prototype search: DOM text stays unchanged and copy remains verbatim.
    search(query) {
      document.querySelectorAll('mark').forEach(n => n.replaceWith(...n.childNodes));
      if (!query) return 0;
      const hits = words.filter(w => w.textContent.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
      for (const word of hits) { const m=document.createElement('mark'); m.textContent=word.textContent; word.replaceChildren(m); }
      hits[0]?.scrollIntoView({block:'center',behavior:'instant'}); return hits.length;
    }
  };
  document.addEventListener('click', event => {
    const button = event.target.closest('button[data-start]');
    if (button && getSelection().isCollapsed) post({kind:'seek',ms:+button.dataset.start});
  });
  document.addEventListener('wheel', () => post({kind:'manual'}), {passive:true});
  document.addEventListener('touchmove', () => post({kind:'manual'}), {passive:true});
  document.addEventListener('keydown', event => {
    if (['PageDown','PageUp','Home','End','ArrowDown','ArrowUp',' '].includes(event.key)) post({kind:'manual'});
  });
  // Scrollbar drags also suspend follow; text selection must stay native.
  document.addEventListener('pointerdown', () => post({kind:'manual'}), {passive:true});
  index();
  requestAnimationFrame(() => requestAnimationFrame(() => post({kind:'ready', height:document.documentElement.scrollHeight, words:words.length})));
})();
