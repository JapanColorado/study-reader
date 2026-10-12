// Confetti for the review page's celebrations: bursts of paper drawn on a canvas over the page
// (by hand, so it works offline). Nothing is drawn when the system asks for reduced motion.
const confetti = (() => {
  const COLORS = ["#e8590c", "#f2c94c", "#3a8f5c", "#2f80ed", "#bb6bd9", "#eb5757", "#56ccf2"];
  let canvas = null, ctx = null, parts = [], running = false;

  function frame() {
    const dpr = window.devicePixelRatio || 1;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    parts = parts.filter(p => p.life > 0 && p.y < innerHeight + 30);
    for (const p of parts) {
      p.vx *= 0.985;
      p.vy = p.vy * 0.985 + 0.22;
      p.x += p.vx + Math.sin(p.tilt) * 0.6;
      p.y += p.vy;
      p.rot += p.vr;
      p.tilt += 0.12;
      p.life--;
      const squash = Math.abs(Math.cos(p.tilt));
      ctx.save();
      ctx.translate(p.x * dpr, p.y * dpr);
      ctx.rotate(p.rot);
      ctx.globalAlpha = Math.min(1, p.life / 30);
      ctx.fillStyle = p.color;
      ctx.fillRect(-p.w / 2 * dpr, -p.h / 2 * dpr * squash, p.w * dpr, p.h * dpr * squash);
      ctx.restore();
    }
    if (parts.length) requestAnimationFrame(frame);
    else { running = false; canvas.style.display = "none"; }
  }

  // x, y: where the burst starts, as fractions of the window; angle: its direction in degrees
  // (90 is up); spread: how wide it fans out; power: how far it flies.
  return function ({ count = 100, x = 0.5, y = 0.35, angle = 90, spread = 70, power = 1 } = {}) {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    if (!canvas) {
      canvas = document.createElement("canvas");
      canvas.style.cssText = "position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:50";
      document.body.appendChild(canvas);
      ctx = canvas.getContext("2d");
    }
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== innerWidth * dpr || canvas.height !== innerHeight * dpr) {
      canvas.width = innerWidth * dpr;
      canvas.height = innerHeight * dpr;
    }
    canvas.style.display = "block";
    for (let i = 0; i < count; i++) {
      const a = (angle + (Math.random() - 0.5) * spread) * Math.PI / 180;
      const v = (5 + Math.random() * 8) * power;
      parts.push({
        x: x * innerWidth, y: y * innerHeight, vx: Math.cos(a) * v, vy: -Math.sin(a) * v,
        w: 5 + Math.random() * 5, h: 8 + Math.random() * 6, rot: Math.random() * 6, vr: (Math.random() - 0.5) * 0.3,
        tilt: Math.random() * 6, life: 140 + Math.random() * 70, color: COLORS[i % COLORS.length],
      });
    }
    if (!running) { running = true; requestAnimationFrame(frame); }
  };
})();
