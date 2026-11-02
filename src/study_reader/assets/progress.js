// Review progress widgets shared by home.html and review.html: the rewards row (gamify.py's
// level, streak and daily goal), the calendar of reviews per day, and the forecast of cards due.
// Needs esc() (markdown.js or the page's own).

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const comma = n => n.toLocaleString();
const isoDay = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

function setTheme(colors, dark) {
  const root = document.documentElement;
  root.dataset.theme = dark ? "dark" : "light";
  for (const [k, v] of Object.entries(colors)) root.style.setProperty(`--${k.replace(/_/g, "-")}`, v);
}

// Level and XP, the streak and the daily goal; a nudge under them when the streak needs a card.
function playerHtml(g) {
  const r = 17, C = 2 * Math.PI * r, frac = Math.min(g.today / g.goal, 1);
  const met = g.goal_met;
  let html = `<div class="player">`
    + `<div class="lvl" title="${comma(g.xp)} XP in all: 10 a card, 15 a new one, times your combo multiplier">`
    + `<div class="top"><span class="badge">Lv ${g.level}</span><span class="title">${esc(g.title)}</span></div>`
    + `<div class="xpbar"><i style="width:${(100 * g.into / g.need).toFixed(1)}%"></i></div>`
    + `<div class="l">${comma(g.into)} / ${comma(g.need)} XP to level ${g.level + 1}</div></div>`
    + `<div class="streak${g.streak ? "" : " off"}" title="Days in a row with at least one review. A streak freeze `
    + `covers a missed day; you earn one every ${g.freeze_every} days of streak (at most 2).">`
    + `<span class="flame">🔥</span><div><div class="n">${g.streak}</div>`
    + `<div class="l">day streak${g.freezes ? ` · ${"❄️".repeat(g.freezes)}` : ""}</div></div></div>`
    + `<div class="ring" title="Daily goal: ${g.goal} reviews (change it under Settings › Review settings). Reviewing everything due meets it too.">`
    + `<svg viewBox="0 0 42 42"><circle class="track" cx="21" cy="21" r="${r}"/>`
    + `<circle class="fill" cx="21" cy="21" r="${r}" stroke-dasharray="${C.toFixed(2)}" stroke-dashoffset="${(C * (1 - (met ? 1 : frac))).toFixed(2)}"/></svg>`
    + `<div><div class="n">${g.today}<small> / ${g.goal}</small></div><div class="l">${met ? "goal met ✓" : "today's goal"}</div></div></div>`
    + `</div>`;
  if (g.at_risk) html += `<div class="nudge" style="margin-top:10px">Keep your ${g.streak}-day streak going: even one card today counts.</div>`;
  else if (g.lapsed) html += `<div class="nudge" style="margin-top:10px">A fresh start: one card begins a new streak.</div>`;
  return html;
}

// Reviews per day, a column a week; days a streak freeze covered show a snowflake.
// opts.size: a cell's side in pixels; opts.fit: show only the latest weeks that fit the box.
function heatmap(el, heat, opts = {}) {
  const counts = Object.fromEntries(heat.days.map(d => [d.date, d.count]));
  const frozen = new Set(heat.frozen);
  const max = Math.max(1, ...heat.days.map(d => d.count));
  const gap = 3, top = 14, left = 26;
  let size = opts.size || 12;
  let start = new Date(heat.start + "T12:00");
  const end = new Date(heat.end + "T12:00");
  if (opts.fit && el.clientWidth) {
    const weeks = Math.max(4, Math.floor((el.clientWidth - left) / (size + gap)));
    const earliest = new Date(end); earliest.setDate(earliest.getDate() - earliest.getDay() - 7 * (weeks - 1));
    if (earliest > start) start = earliest;
    // Room to spare: bigger cells, up to opts.max.
    const cols = Math.round((end - start) / (7 * 86400000)) + 2;
    size = Math.max(size, Math.min(opts.max || size, Math.floor((el.clientWidth - left) / cols) - gap));
  }
  const first = new Date(start); first.setDate(first.getDate() - first.getDay());
  let cells = "", months = "", x = 0, lastMonth = -1, lastLabel = -9;
  for (let week = new Date(first); week <= end; week.setDate(week.getDate() + 7), x++) {
    if (week.getMonth() !== lastMonth) {
      lastMonth = week.getMonth();
      if (x - lastLabel >= 3) {  // else too close to the last month's name to fit one
        lastLabel = x;
        months += `<text x="${left + x * (size + gap)}" y="10" font-size="10" style="fill:var(--muted)">${week.toLocaleDateString(undefined, { month: "short" })}</text>`;
      }
    }
    for (let d = 0; d < 7; d++) {
      const day = new Date(week); day.setDate(day.getDate() + d);
      if (day < start || day > end) continue;
      const iso = isoDay(day), n = counts[iso] || 0;
      const px = left + x * (size + gap), py = top + d * (size + gap);
      const label = day.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
      if (frozen.has(iso)) {
        cells += `<text x="${px + size / 2}" y="${py + size - 2}" font-size="${size - 1}" text-anchor="middle"><title>${esc(label)}: streak freeze</title>❄</text>`;
        continue;
      }
      const opacity = n ? (0.3 + 0.7 * Math.sqrt(n / max)).toFixed(2) : 0.55;
      const style = `fill:var(${n ? "--accent" : "--border"})${iso === heat.end ? ";stroke:var(--muted)" : ""}`;
      cells += `<rect x="${px}" y="${py}" width="${size}" height="${size}" rx="2.5" style="${style}"`
        + ` fill-opacity="${opacity}"><title>${esc(label)}: ${plural(n, "review")}</title></rect>`;
    }
  }
  const days = ["", "Mon", "", "Wed", "", "Fri", ""].map((l, d) => l ? `<text x="0" y="${top + d * (size + gap) + size - 2}" font-size="10" style="fill:var(--muted)">${l}</text>` : "").join("");
  el.innerHTML = `<svg width="${left + x * (size + gap)}" height="${top + 7 * (size + gap)}">${months}${days}${cells}</svg>`;
}

// Cards due on each of the coming days (dashboard.forecast), as bars.
function forecastHtml(days) {
  const max = Math.max(1, ...days.map(d => d.count));
  return `<div class="forecast">` + days.map((d, i) => {
    const day = new Date(d.date + "T12:00");
    const label = i === 0 ? "Tmrw" : day.toLocaleDateString(undefined, { weekday: "narrow" });
    const title = `${day.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}: ${plural(d.count, "card")}`;
    return `<div class="col" title="${esc(title)}"><span class="v">${d.count || ""}</span>`
      + `<div class="bar${d.count ? "" : " zero"}" style="height:${d.count ? Math.max(4, 70 * d.count / max) : 2}px"></div>`
      + `<span class="d">${label}</span></div>`;
  }).join("") + `</div>`;
}
