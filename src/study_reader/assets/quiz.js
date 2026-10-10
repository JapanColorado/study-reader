// Quiz questions as transcript.html (quizzes in replies) and review.html (review cards) draw
// them. Needs markdown.js and mathfield.js loaded first; each page wires up its own events.
const LETTERS = "ABCDEFGHIJ";
const VERDICT_LABELS = { correct: "✓ Correct", partial: "◐ Partly right", incorrect: "✗ Not quite" };
const RATING_LABELS = { 1: "Again", 2: "Hard", 3: "Good", 4: "Easy" };

function tex(latex) {
  try { return katex.renderToString(latex, { throwOnError: false }); } catch (e) { return esc(latex); }
}

// One question; a is its answer ({response, status, verdict, feedback}) once it has one.
// opts: index (data-q), meta (HTML for the line above it), draft (typed but not submitted),
// revealed (a flashcard's back is showing; with rate, the rating buttons under it),
// freeActions (HTML for the row under a free-response box), openAnswer (show a free
// response's model answer outright rather than folded away).
function questionHtml(q, a, opts = {}) {
  const draft = opts.draft || "";
  let html = (opts.meta ? `<div class="q-meta">${opts.meta}</div>` : "") + `<div class="q-text">${renderMarkdown(q.question)}</div>`;
  const r = a ? a.response || {} : {};
  const option = (label, k, value, chosen) => {
    let cls = "choice";
    if (a) cls += value === q.answer ? " right" : chosen ? " wrong" : " dim";
    return `<button class="${cls}" data-act="choose" data-k="${k}"${a ? " disabled" : ""}>`
      + (label ? `<span class="letter">${label}</span>` : "") + `<span class="c-text">${value === true ? "True" : value === false ? "False" : renderMarkdown(q.choices[k], true)}</span></button>`;
  };
  if (q.type === "choice") {
    html += `<div class="choices">${q.choices.map((_, k) => option(LETTERS[k], k, k, r.choice === k)).join("")}</div>`;
  } else if (q.type === "truefalse") {
    html += `<div class="choices tf">${[true, false].map((v, k) => option("", k, v, r.value === v)).join("")}</div>`;
  } else if (q.type === "numeric") {
    const unit = q.unit ? ` <span class="unit">${renderMarkdown(q.unit, true)}</span>` : "";
    if (a) {
      html += `<div class="given"><span class="label">Your answer</span>${tex(r.latex || "")}${unit}</div>`;
      if (a.verdict !== "correct") html += `<div class="expected"><span class="label">Answer</span>${tex(q.answer_latex)}${unit}</div>`;
    } else {
      html += `<div class="answer-row"><math-field math-virtual-keyboard-policy="manual">${esc(draft)}</math-field>${unit}`
        + `<button class="qbtn" data-act="numeric">Check</button></div>`
        + `<div class="submit-row">Enter to check · / for a fraction, ^ for a power</div><div class="q-error"></div>`;
    }
  } else if (q.type === "flashcard") {
    if (!a && opts.revealed) {
      html += `<div class="back">${renderMarkdown(q.answer)}</div>`;
      if (opts.rate) html += `<div class="submit-row">How well did you know it?</div>${ratingRow()}`;
    } else if (!a) {
      html += `<div class="submit-row"><span class="grow">Recall the answer, then reveal it</span>`
        + `<button class="qbtn" data-act="reveal">Show answer</button></div>`;
    }
  } else if (a) {
    if (r.text) html += `<div class="given">${esc(r.text)}</div>`;
  } else {
    html += `<textarea class="free" rows="3" placeholder="Your answer…">${esc(draft)}</textarea>`
      + (opts.freeActions ?? `<div class="submit-row"><span class="grow">Ctrl+Enter to submit · Claude grades it</span>`
        + `<button class="qbtn" data-act="free">Submit</button></div>`);
  }
  if (a) html += resultHtml(q, a, opts);
  return `<div class="q${a ? " answered" : ""}" data-q="${opts.index ?? 0}">${html}</div>`;
}

function resultHtml(q, a, opts = {}) {
  if (a.status === "pending") return `<div class="verdict pending"><span class="spinner"></span>Claude is grading…</div>`;
  if (a.status === "error")
    return `<div class="verdict error">Grading failed: ${esc(a.feedback || "unknown error")}`
      + ` <button class="qbtn quiet" data-act="regrade">Try again</button></div>`;
  let html = "";
  const rating = a.response && a.response.rating;
  if (q.type === "flashcard") {
    html += `<div class="back">${renderMarkdown(q.answer)}</div>`;
    if (rating) html += `<div class="verdict ${a.verdict || "none"}">You rated it ${RATING_LABELS[rating]}</div>`;
  } else if (a.verdict || a.feedback) {
    html += `<div class="verdict ${a.verdict || "none"}">${VERDICT_LABELS[a.verdict] || "Feedback"}</div>`;
  }
  if (a.feedback) html += `<div class="feedback">${renderMarkdown(a.feedback)}</div>`;
  if (q.explanation) html += `<div class="explain">${renderMarkdown(q.explanation)}</div>`;
  if (q.type === "free" && q.answer) {
    html += opts.openAnswer
      ? `<div class="model-answer"><span class="label">Model answer</span><div>${renderMarkdown(q.answer)}</div></div>`
      : `<details><summary>Model answer</summary>${renderMarkdown(q.answer)}</details>`;
  }
  return html;
}

// Again / Hard / Good / Easy. previews: {rating: "10m"} for when each brings the card back;
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
  if (act === "numeric") {
    const latex = qEl.querySelector("math-field").value.trim();
    return latex ? { latex } : null;
  }
  const text = qEl.querySelector("textarea").value.trim();
  return text ? { text } : null;
}
