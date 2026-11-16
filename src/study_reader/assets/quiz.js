// Quiz questions as transcript.html (quizzes in replies), practice.html (practice) and
// questionview.html (the question browser's preview) draw them. Needs markdown.js and
// mathfield.js loaded first; each page wires up its own events.
const LETTERS = "ABCDEFGHIJ";
const VERDICT_LABELS = { correct: "✓ Correct", partial: "◐ Partly right", incorrect: "✗ Not quite" };
const RATING_LABELS = { 1: "Missed", 2: "Shaky", 3: "Got it", 4: "Easy" };

// The kinds of question (question_model.TYPES) and what each is called.
const TYPE_LABELS = {
  self_check: "Self-check", both_ways: "Self-check (both ways)", fill_in: "Fill-in-the-blank", image_labels: "Image labels",
  choice: "Multiple choice", truefalse: "True/false", calculation: "Calculation", short_answer: "Short answer",
};
// Older names of the kinds, still in quizzes in older replies (quiz.py reads them too).
const TYPE_ALIASES = {
  flashcard: "self_check", card: "self_check", basic: "self_check", reversed: "both_ways", reverse: "both_ways",
  cloze: "fill_in", occlusion: "image_labels", numeric: "calculation", written: "short_answer", free: "short_answer",
};
const kindName = kind => TYPE_ALIASES[kind] || kind;
const kindLabel = kind => TYPE_LABELS[kindName(kind)] || kind;

// A question as it is drawn: its type under its current name, and one side of a both-ways
// self-check drawn as a self-check.
function drawnQuestion(q) {
  let type = kindName(q.type);
  if (type === "both_ways") type = "self_check";
  return type === q.type ? q : { ...q, type };
}

// Types the learner rates on their own once the answer is showing (question_model.SELF_RATED).
const SELF_RATED = ["self_check", "fill_in", "image_labels"];

// A question's Markdown; questions from the browser or Anki (q.html) may hold HTML.
function mdq(q, text, inline = false) { return renderMarkdown(text, inline, !!q.html); }

function tex(latex) {
  try { return katex.renderToString(latex, { throwOnError: false }); } catch (e) { return esc(latex); }
}

// One question; a is its answer ({response, status, verdict, feedback}) once it has one.
// opts: index (data-q), meta (HTML for the line above it), draft (typed but not submitted),
// revealed (a self-check's answer is showing; with rate, the rating buttons under it),
// answerActions (HTML for the row under a short answer's box), openAnswer (show a short
// answer's model answer and rubric outright, for rating it yourself, rather than folded away).
function questionHtml(q, a, opts = {}) {
  q = drawnQuestion(q);
  const draft = opts.draft || "";
  const showing = !!(a || opts.revealed);  // the answer side of a self-rated question
  const front = q.type === "fill_in" && showing ? q.answer : q.question;
  let html = (opts.meta ? `<div class="q-meta">${opts.meta}</div>` : "")
    + (front || q.type !== "image_labels" ? `<div class="q-text">${mdq(q, front)}</div>` : "");
  if (q.type === "image_labels") html += imageLabelsHtml(q, showing);
  const r = a ? a.response || {} : {};
  const option = (label, k, value, chosen) => {
    let cls = "choice";
    if (a) cls += value === q.answer ? " right" : chosen ? " wrong" : " dim";
    return `<button class="${cls}" data-act="choose" data-k="${k}"${a ? " disabled" : ""}>`
      + (label ? `<span class="letter">${label}</span>` : "") + `<span class="c-text">${value === true ? "True" : value === false ? "False" : mdq(q, q.choices[k], true)}</span></button>`;
  };
  if (q.type === "choice") {
    html += `<div class="choices">${q.choices.map((_, k) => option(LETTERS[k], k, k, r.choice === k)).join("")}</div>`;
  } else if (q.type === "truefalse") {
    html += `<div class="choices tf">${[true, false].map((v, k) => option("", k, v, r.value === v)).join("")}</div>`;
  } else if (q.type === "calculation") {
    const unit = q.unit ? ` <span class="unit">${mdq(q, q.unit, true)}</span>` : "";
    if (a) {
      html += `<div class="given"><span class="label">Your answer</span>${tex(r.latex || "")}${unit}</div>`;
      if (a.verdict !== "correct") html += `<div class="expected"><span class="label">Answer</span>${tex(q.answer_latex)}${unit}</div>`;
    } else {
      html += `<div class="answer-row"><math-field math-virtual-keyboard-policy="manual">${esc(draft)}</math-field>${unit}`
        + `<button class="qbtn" data-act="calculation">Check</button></div>`
        + `<div class="submit-row">Enter to check · / for a fraction, ^ for a power</div><div class="q-error"></div>`;
    }
  } else if (SELF_RATED.includes(q.type)) {
    if (!a && opts.revealed) {
      if (q.type === "self_check") html += `<div class="back">${mdq(q, q.answer)}</div>`;
      else if (q.explanation) html += `<div class="explain">${mdq(q, q.explanation)}</div>`;
      if (opts.rate) html += `<div class="submit-row">How well did you know it?</div>${ratingRow()}`;
    } else if (!a) {
      html += `<div class="submit-row"><span class="grow">Recall the answer, then reveal it</span>`
        + `<button class="qbtn" data-act="reveal">Show answer</button></div>`;
    }
  } else if (a) {
    if (r.text) html += `<div class="given">${esc(r.text)}</div>`;
  } else {
    html += `<textarea class="short-answer" rows="3" placeholder="Your answer…">${esc(draft)}</textarea>`
      + (opts.answerActions ?? `<div class="submit-row"><span class="grow">Ctrl+Enter to submit · Claude grades it</span>`
        + `<button class="qbtn" data-act="short_answer">Submit</button></div>`);
  }
  if (a) html += resultHtml(q, a, opts);
  return `<div class="q${a ? " answered" : ""}" data-q="${opts.index ?? 0}">${html}</div>`;
}

function resultHtml(q, a, opts = {}) {
  q = drawnQuestion(q);
  if (a.status === "pending") return `<div class="verdict pending"><span class="spinner"></span>Claude is grading…</div>`;
  if (a.status === "error")
    return `<div class="verdict error">Grading failed: ${esc(a.feedback || "unknown error")}`
      + ` <button class="qbtn quiet" data-act="regrade">Try again</button></div>`;
  let html = "";
  const rating = a.response && a.response.rating;
  if (SELF_RATED.includes(q.type)) {
    if (q.type === "self_check") html += `<div class="back">${mdq(q, q.answer)}</div>`;
    if (rating) html += `<div class="verdict ${a.verdict || "none"}">You rated it ${RATING_LABELS[rating]}</div>`;
  } else if (a.verdict || a.feedback) {
    html += `<div class="verdict ${a.verdict || "none"}">${VERDICT_LABELS[a.verdict] || "Feedback"}</div>`;
  }
  if (a.feedback) html += `<div class="feedback">${renderMarkdown(a.feedback)}</div>`;
  if (q.type === "short_answer" && opts.openAnswer) {
    if (q.answer) html += `<div class="model-answer"><span class="label">Model answer</span><div>${mdq(q, q.answer)}</div></div>`;
    if (q.rubric) html += `<div class="model-answer"><span class="label">A full answer covers</span><div>${mdq(q, q.rubric)}</div></div>`;
  }
  if (q.explanation) html += `<div class="explain">${mdq(q, q.explanation)}</div>`;
  if (q.type === "short_answer" && q.answer && !opts.openAnswer) html += `<details><summary>Model answer</summary>${mdq(q, q.answer)}</details>`;
  return html;
}

// An image with shapes masked over it (Anki's image occlusion). The question's own shapes
// (q.active) are hidden until the answer shows, then outlined; the others stay covered if they
// were made "hide all, guess one" (oi), else aren't drawn.
function imageLabelsHtml(q, showing) {
  const shapes = (q.shapes || []).map(s => {
    const mine = s.ord === q.active;
    if (!mine && !s.oi) return "";
    const cls = mine ? (showing ? "mask mine shown" : "mask mine") : "mask other";
    if (s.shape === "ellipse") {
      const rx = (s.rx ?? s.width / 2), ry = (s.ry ?? s.height / 2);
      return `<ellipse class="${cls}" cx="${s.left + rx}" cy="${s.top + ry}" rx="${rx}" ry="${ry}"/>`;
    }
    if (s.shape === "polygon" && s.points) {
      return `<polygon class="${cls}" points="${s.points.map(p => p.join(",")).join(" ")}"/>`;
    }
    if (s.shape === "text") return "";
    return `<rect class="${cls}" x="${s.left}" y="${s.top}" width="${s.width}" height="${s.height}"/>`;
  }).join("");
  return `<div class="image-labels"><img src="${esc(mediaUrl(q.image || ""))}">`
    + `<svg viewBox="0 0 1 1" preserveAspectRatio="none">${shapes}</svg></div>`;
}

// The order to draw a multiple-choice question's options in, mixed anew on every call:
// order[k] is the index in q.choices of the k-th option drawn. Choices that speak of the others
// ("none of the above") keep their places, and nothing moves when a choice or the explanation
// names options by letter ("Both A and B", "Option C is…").
const CHOICE_LETTERS = /\b(?:[Oo]ptions?|[Cc]hoices?|[Aa]nswers?) \(?[A-E]\b|\b[A-E]\)?,? (?:and|or|&) \(?[A-E]\b/;
const CHOICE_PLACED = /\b(?:of the above|of these|of those|of the others?|the above)\b/i;
function choiceOrder(q, random = Math.random) {
  const order = q.choices.map((_, k) => k);
  if (CHOICE_LETTERS.test([q.explanation || "", ...q.choices].join("\n"))) return order;
  const free = order.filter(k => !CHOICE_PLACED.test(q.choices[k]));
  const mixed = free.slice();
  for (let i = mixed.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    [mixed[i], mixed[j]] = [mixed[j], mixed[i]];
  }
  free.forEach((k, i) => { order[k] = mixed[i]; });
  return order;
}

// Missed / Shaky / Got it / Easy. previews: {rating: "10m"} for when each brings the question back;
// suggested: the rating the answer's grade suggests (Space or Enter picks it).
function ratingRow(previews = null, suggested = null) {
  return `<div class="rate-row">` + [1, 2, 3, 4].map(k => {
    const when = previews ? `<span class="when">${esc(previews[k] || "")}</span>` : "";
    const key = previews ? `<span class="key">${k}</span>` : "";
    return `<button class="rate r${k}${k === suggested ? " suggested" : ""}" data-act="rate" data-k="${k}">`
      + `<span>${RATING_LABELS[k]}</span>${when}${key}</button>`;
  }).join("") + `</div>`;
}

// Math fields in freshly drawn questions: the calculator's typing, Enter to check.
function setupMathFields(root, onEnter, onInput) {
  for (const mf of root.querySelectorAll(".q math-field")) {
    configureMathField(mf);
    mf.addEventListener("keydown", ev => {
      if (ev.key !== "Enter") return;
      ev.preventDefault(); ev.stopPropagation();
      onEnter(mf.closest(".q"));
    }, { capture: true });
    if (onInput) mf.addEventListener("input", () => onInput(mf));
  }
}

// The response a question's inputs hold for a button's action; null if there is none yet.
function readResponse(q, qEl, act, button) {
  if (act === "choose") {
    const k = +button.dataset.k;
    return q.type === "truefalse" ? { value: k === 0 } : { choice: k };
  }
  if (act === "rate") return { rating: +button.dataset.k };
  if (act === "calculation") {
    const latex = qEl.querySelector("math-field").value.trim();
    return latex ? { latex } : null;
  }
  const text = qEl.querySelector("textarea").value.trim();
  return text ? { text } : null;
}
