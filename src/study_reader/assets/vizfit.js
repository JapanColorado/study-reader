// Fits a visualization's sandboxed frame (made by vizDoc in transcript.html) into the
// space it has. The frame reports its content's size; when that is too tall, the frame
// is laid out narrower (plots drawn to the width get shorter with it), and whatever
// still doesn't fit, or is wider than the space, is scaled down. The visualization
// window does the same inside the frame instead (vizWindowMode), so it can keep the
// visualization's controls in view.
const VIZ_MIN_WIDTH = 300;
const VIZ_FIT_TRIES = 6;
const VIZ_MAX_LAYOUT = 4000;  // tallest frame laid out, against content sized to the frame

class VizFit {
  // box holds the frame (position:relative); avail() gives the {width, height} to fit.
  constructor(frame, box, avail) {
    Object.assign(this, { frame, box, avail, size: null, growths: 0 });
    frame.style.position = "absolute";
    frame.style.top = "0";
    frame.style.transformOrigin = "0 0";
    this.refit();
  }

  // Fit again from the full width, after the space changed.
  refit() {
    const { width, height } = this.avail();
    if (this.fitted && this.fitted.width === width && this.fitted.height === height) return;
    this.fitted = { width, height };
    this.tries = 0;
    this.prev = null;
    this.widened = false;
    const full = Math.max(1, Math.floor(width));
    const same = full === this.width;
    this.layout(full);
    // A new width brings a new report; at the same width, fit what was reported.
    if (this.size) same ? this.step() : this.place();
  }

  layout(width) {
    this.width = width;
    this.frame.style.width = `${width}px`;
  }

  // The frame's content is size.width × size.height at the current layout width.
  report(size) {
    if (Math.abs(size.viewport - this.width) > 1) return;  // measured before the last layout
    // Content sized to the frame's own height (vh, height:100%) outgrows the frame
    // right after every resize; lay it out at the space's height instead of chasing it.
    const grew = this.laidHeight && performance.now() - this.placedAt < 200 ? size.height - this.laidHeight : 0;
    this.growths = grew > 0 && grew === this.lastGrowth ? this.growths + 1 : 0;
    this.lastGrowth = grew;
    if (this.growths >= 2) this.followsFrame = true;
    this.size = size;
    this.step();
  }

  step() {
    const size = this.size;
    const { height } = this.avail();
    if (size.width > this.width + 1 && !this.widened) {
      // Fixed-width content: give it its width once, then scale. Content that overflows
      // whatever width it gets (labels past a full-width plot) would otherwise grow forever.
      this.widened = true;
      this.layout(Math.ceil(size.width));
    } else if (!this.followsFrame && size.height > height + 1 && this.tries < VIZ_FIT_TRIES) {
      if (this.prev && size.height >= this.prev.height * 0.97) {
        // Narrower didn't make it shorter: go back and scale instead.
        this.tries = VIZ_FIT_TRIES;
        this.layout(this.prev.width);
        return;
      }
      const next = Math.max(VIZ_MIN_WIDTH, Math.floor(this.width * height / size.height));
      if (next < this.width - 2) {
        this.prev = { width: this.width, height: size.height };
        this.tries++;
        this.widened = false;
        this.layout(next);
        return;  // its new size comes in the next report
      }
    }
    this.place();
  }

  place() {
    const { width, height } = this.avail();
    const w = Math.max(this.width, this.size.width);
    const h = this.followsFrame ? Math.max(height, 40) : Math.min(Math.max(this.size.height, 40), VIZ_MAX_LAYOUT);
    this.laidHeight = h;
    this.placedAt = performance.now();
    const s = Math.min(1, width / w, height / h);
    Object.assign(this.frame.style, {
      height: `${h}px`, transform: s === 1 ? "" : `scale(${s})`, left: `${Math.max(0, (width - w * s) / 2)}px`,
    });
    this.box.style.height = `${h * s}px`;
  }
}

// Runs inside a visualization's frame in the visualization window, which fills the
// window and scrolls itself. Its controls (inputs and what holds them, short of a plot)
// move to a dock that stays in view, at the left or the top; the rest is fitted to the
// window like VizFit does, then zoomed with CSS zoom, by Ctrl+wheel, the zoom keys, or
// the window's toolbar (vizCmd messages). It reports its scale (vizScale), and passes up
// F11 and Escape (vizKey). Inlined into the frame by vizDoc, so it uses nothing outside.
function vizWindowMode() {
  const PAD = 16, MIN_WIDTH = 300, SIDE_DOCK = 260, ZOOM_MIN = 0.1, ZOOM_MAX = 8, STEP = 1.25;
  const root = document.documentElement, body = document.body;
  let zoom = null, scale = 1, fitScale = 1, width = 0, size = { width: 0, height: 0 }, dock = null, refits = 0;

  function buildDock() {
    const figure = "svg, canvas, img, video, table";
    // Without a plot to keep apart (a visualization drawn with divs), the controls'
    // containers would take the whole visualization into the dock.
    if (!body.querySelector(figure)) return null;
    const groups = [];
    for (const el of body.querySelectorAll("input, select, textarea, button")) {
      let g = el;
      while (g.parentElement && g.parentElement !== body && !g.parentElement.querySelector(figure)) g = g.parentElement;
      if (g.querySelector(figure) || g.matches(figure)) continue;
      // A bare control among the plots: take the label before it along.
      if (g === el && g.previousElementSibling?.matches("label, span")) g = [g.previousElementSibling, g];
      groups.push(...[g].flat());
    }
    const unique = [...new Set(groups)].filter(g => !groups.some(o => o !== g && o.contains(g)))
      .sort((a, b) => a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
    if (!unique.length) return null;
    const d = document.createElement("div");
    d.id = "viz-dock";
    // Each group goes into copies of the elements it was in (tag and classes), so the
    // visualization's own styles (".wrap label") still apply; siblings share them.
    const shells = new Map();
    for (const g of unique) {
      let shell = shells.get(g.parentElement);
      if (!shell) {
        shell = d;
        const chain = [];
        for (let a = g.parentElement; a && a !== body; a = a.parentElement) chain.unshift(a);
        for (const a of chain) {
          const copy = document.createElement(a.tagName);
          copy.className = a.className;
          shell = shell.appendChild(copy);
        }
        shells.set(g.parentElement, shell);
      }
      shell.append(g);
    }
    root.append(d);  // outside the body, so the body's zoom leaves it alone
    return d;
  }

  const style = document.createElement("style");
  style.textContent = `html{overflow:auto;box-sizing:border-box;} body{box-sizing:border-box;margin:0;}
#viz-dock{position:fixed;z-index:10;overflow:auto;box-sizing:border-box;background:var(--surface);
  font:13.5px/1.5 system-ui,sans-serif;color:var(--fg);padding:10px 12px;}
#viz-dock.side{left:0;top:0;bottom:0;width:${SIDE_DOCK}px;border-right:1px solid var(--border);}
#viz-dock.top{left:0;right:0;top:0;max-height:40vh;border-bottom:1px solid var(--border);}
#viz-dock > *{margin:0;max-width:none;} #viz-dock input[type=range]{max-width:100%;}`;
  document.head.append(style);

  function space() {
    const side = dock && dock.classList.contains("side");
    const left = side ? SIDE_DOCK : 0, top = dock && !side ? dock.offsetHeight : 0;
    root.style.padding = `${top + PAD}px ${PAD}px ${PAD}px ${left + PAD}px`;
    return { width: root.clientWidth - left - 2 * PAD, height: root.clientHeight - top - 2 * PAD };
  }

  function measure(w) {
    body.style.width = `${w}px`;
    return { width: Math.max(w, body.scrollWidth), height: body.scrollHeight };
  }

  // Lay the body out at the width that fits the window best, as VizFit does.
  function fit() {
    if (dock) dock.className = innerWidth >= 900 ? "side" : "top";
    body.style.zoom = 1;
    body.style.margin = "0";
    const avail = space();
    width = Math.max(1, Math.floor(avail.width));
    size = measure(width);
    for (let i = 0; i < 6 && size.height > avail.height + 1 && size.width <= width + 1; i++) {
      const next = Math.max(MIN_WIDTH, Math.floor(width * avail.height / size.height));
      if (next >= width - 2) break;
      const smaller = measure(next);
      if (smaller.height >= size.height * 0.97) { measure(width); break; }  // not shorter: scale instead
      width = next;
      size = smaller;
    }
    if (size.width > width) size = measure(width = Math.ceil(size.width));
    fitScale = Math.min(1, avail.width / size.width, avail.height / size.height);
    apply();
  }

  function apply() {
    const avail = space();
    scale = zoom ?? fitScale;
    body.style.zoom = scale;
    // Margins are in the body's zoomed pixels.
    body.style.marginLeft = `${Math.max(0, (avail.width - size.width * scale) / 2) / scale}px`;
    body.style.marginTop = `${Math.max(0, (avail.height - size.height * scale) / 2) / scale}px`;
    parent.postMessage({ vizScale: { scale, fit: zoom === null } }, "*");
  }

  // Zoom keeping the point under (x, y) in the window where it is.
  function zoomTo(z, x = root.clientWidth / 2, y = root.clientHeight / 2) {
    if (z !== null) z = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));
    const before = body.getBoundingClientRect(), s = scale;
    const px = (x - before.left) / s, py = (y - before.top) / s;
    zoom = z;
    apply();
    const after = body.getBoundingClientRect();
    scrollBy(after.left + px * scale - x, after.top + py * scale - y);
  }
  const commands = {
    in: () => zoomTo(scale * STEP), out: () => zoomTo(scale / STEP), fit: () => zoomTo(null), actual: () => zoomTo(1),
  };

  addEventListener("wheel", e => {
    if (!e.ctrlKey || e.target.closest?.("#viz-dock")) return;
    e.preventDefault();
    zoomTo(scale * Math.exp(-e.deltaY * 0.0015), e.clientX, e.clientY);
  }, { passive: false });
  addEventListener("keydown", e => {
    const ctrl = e.ctrlKey || e.metaKey;
    const command = ctrl && { "=": "in", "+": "in", "-": "out", "0": "fit", "1": "actual" }[e.key];
    if (command) { e.preventDefault(); commands[command](); }
    else if (e.key === "F11" || e.key === "Escape") {
      if (e.key === "F11") e.preventDefault();
      parent.postMessage({ vizKey: e.key }, "*");
    }
  });
  addEventListener("message", e => { if (e.source === parent && commands[e.data?.vizCmd]) commands[e.data.vizCmd](); });

  // Drag empty space to pan.
  let drag = null;
  addEventListener("pointerdown", e => {
    const t = e.target;
    if (e.button !== 1 && !(e.button === 0 && (t === root || t === body))) return;
    drag = { x: e.clientX, y: e.clientY };
    root.style.cursor = "grabbing";
    e.preventDefault();
  });
  const endDrag = () => { drag = null; root.style.cursor = ""; };
  addEventListener("pointermove", e => {
    if (!drag) return;
    if (!(e.buttons & 5)) return endDrag();  // released outside the frame
    scrollBy(drag.x - e.clientX, drag.y - e.clientY);
    drag = { x: e.clientX, y: e.clientY };
  });
  addEventListener("pointerup", endDrag);

  let pending = 0;
  const refit = () => { cancelAnimationFrame(pending); pending = requestAnimationFrame(fit); };
  addEventListener("resize", () => { refits = 0; refit(); });
  // Plots drawn by script may redraw at the new width after a fit; fit them again.
  new ResizeObserver(() => {
    const now = { width: Math.max(width, body.scrollWidth), height: body.scrollHeight };
    if (refits < 4 && (Math.abs(now.height - size.height) > 2 || Math.abs(now.width - size.width) > 2)) {
      refits++;
      zoom === null ? refit() : (size = now, apply());
    }
  }).observe(body);
  const start = () => { dock = buildDock(); refits = 0; fit(); };
  document.readyState === "complete" ? start() : addEventListener("load", start);
}
