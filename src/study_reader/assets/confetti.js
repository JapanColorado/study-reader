// Confetti for the review page's celebrations: paper raining down from above the top edge
// (drawn by hand, so it works offline). Nothing is shown when the system asks for reduced motion.
// Each piece is a small element moved by the Web Animations API rather than redrawn from script:
// the page renders without a GPU, and transform animations then run on the compositor, so the
// fall stays smooth even while the page is busy laying out the next card.
const confetti = (() => {
  const COLORS = ["#e8590c", "#f2c94c", "#3a8f5c", "#2f80ed", "#bb6bd9", "#eb5757", "#56ccf2"];
  const SPEED = [420, 640];   // fall speed range, px/s
  let layer = null;

  const rand = (a, b) => a + Math.random() * (b - a);

  // count: how many pieces; spread: over how many ms they start falling.
  return function ({ count = 100, spread = 700 } = {}) {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    if (!layer) {
      layer = document.createElement("div");
      layer.style.cssText = "position:fixed;inset:0;overflow:hidden;pointer-events:none;z-index:50";
      document.body.appendChild(layer);
    }
    const W = innerWidth, H = innerHeight;
    for (let i = 0; i < count; i++) {
      const piece = document.createElement("div");
      const paper = document.createElement("div");
      const w = rand(5, 10), h = rand(8, 14);
      piece.style.cssText = "position:absolute;left:0;top:0;will-change:transform";
      paper.style.cssText = `width:${w}px;height:${h}px;background:${COLORS[i % COLORS.length]}`;
      piece.appendChild(paper);
      layer.appendChild(piece);

      // The fall: straight down with a drift and a sway, as keyframes so it runs off the main thread.
      const x0 = rand(-20, W + 20), y0 = -20 - h, y1 = H + 20;
      const drift = rand(-60, 60), sway = rand(8, 26), turns = rand(1.5, 3), phase = rand(0, 6.3);
      const duration = (y1 - y0) / rand(...SPEED) * 1000, steps = 12, frames = [];
      for (let k = 0; k <= steps; k++) {
        const t = k / steps;
        const x = x0 + drift * t + sway * Math.sin(phase + t * turns * 2 * Math.PI);
        const y = y0 + (y1 - y0) * (0.4 * t + 0.6 * t * t);   // speeding up a little
        frames.push({ transform: `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)` });
      }
      const fall = piece.animate(frames, { duration, delay: rand(0, spread), easing: "linear", fill: "backwards" });
      fall.onfinish = () => piece.remove();

      // The flutter: spinning while it flips over (scaleY through zero reads as a turn in 3D).
      const r = rand(0, 360), dir = Math.random() < 0.5 ? -1 : 1;
      paper.animate([
        { transform: `rotate(${r}deg) scaleY(1)` },
        { transform: `rotate(${r + dir * 180}deg) scaleY(-1)` },
        { transform: `rotate(${r + dir * 360}deg) scaleY(1)` },
      ], { duration: rand(700, 1300), iterations: Infinity });
    }
  };
})();
