// Markdown with $math$ (KaTeX), shared by transcript.html, notes.html and the card pages.
// Needs katex and markdown-it loaded first.

const md = markdownit({ html: false, linkify: true });
// Card text (made in the browser or migrated from Anki) may hold inline HTML, and a line break
// is a break, as in Anki.
const mdRich = markdownit({ html: true, linkify: true, breaks: true });
// Where card images live: a file:// URL ending in "/", set by the page's app side (setMedia).
let MEDIA_URL = "";

const MATH = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|(?<!\\)\$(?!\s)([^$\n]+?)(?<![\s\\])\$(?!\d)/g;
const CODE = /(```[\s\S]*?(?:```|$)|`[^`\n]*`)/;
// Cloze deletions inside math are marked with \htmlClass{cloze}{...}; nothing else is trusted.
const KATEX_RICH = { throwOnError: false, strict: false, trust: ctx => ctx.command === "\\htmlClass" };

function esc(s) { return s.replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

function setMedia(url) { MEDIA_URL = url; }

// Math is lifted out before Markdown runs so underscores and backslashes survive.
// rich: allow inline HTML (sanitized) and point relative image paths at the media folder.
function renderMarkdown(src, inline = false, rich = false) {
  const store = [];
  const parts = String(src ?? "").split(CODE).map((part, i) => {
    if (i % 2) return part;
    return part.replace(MATH, (m, d1, d2, i1, i2) => {
      const display = d1 !== undefined || d2 !== undefined;
      store.push({ tex: d1 ?? d2 ?? i1 ?? i2, display });
      return `@@MATH${store.length - 1}@@`;
    });
  });
  const engine = rich ? mdRich : md;
  let html = (inline ? engine.renderInline : engine.render).call(engine, parts.join(""));
  if (rich) html = sanitize(html);
  return html.replace(/@@MATH(\d+)@@/g, (m, n) => {
    const { tex, display } = store[+n];
    const opts = rich ? { ...KATEX_RICH, displayMode: display } : { displayMode: display, throwOnError: false };
    try { return katex.renderToString(rich ? unescapeHtml(tex) : tex, opts); }
    catch (e) { return esc(tex); }
  });
}

// Entities typed inside math in HTML-ish card text (&lt; for <) mean the character.
const ENTITIES = { lt: "<", gt: ">", amp: "&", quot: '"', apos: "'", nbsp: " " };
function unescapeHtml(s) {
  return s.replace(/&(lt|gt|amp|quot|apos|nbsp|#\d+|#x[0-9a-f]+);/gi, (m, e) => {
    const k = e.toLowerCase();
    if (k[0] !== "#") return ENTITIES[k];
    return String.fromCodePoint(k[1] === "x" ? parseInt(k.slice(2), 16) : parseInt(k.slice(1), 10));
  });
}

// Card HTML can come from shared Anki decks: keep formatting, drop anything that could run
// code. An allowlist, rebuilt into fresh elements, so nothing parsed one way in the inert
// template can be read another way in the page.
const ALLOWED = new Set(("a b i u s em strong small big mark sub sup code kbd samp var pre span div p br hr wbr "
  + "ul ol li dl dt dd blockquote h1 h2 h3 h4 h5 h6 img table thead tbody tfoot tr td th caption colgroup col "
  + "font center ruby rt rp details summary figure figcaption abbr cite q del ins").split(" "));
// Dropped with everything inside them; other unknown elements keep their content.
const DROPPED = new Set(("script style iframe frame frameset object embed applet link meta base form input button "
  + "textarea select option noscript noembed noframes xmp template svg math title audio video source track canvas "
  + "plaintext listing").split(" "));
const ATTRS = new Set(["class", "style", "title", "align", "valign", "colspan", "rowspan", "width", "height", "color",
  "size", "face", "start", "type", "href", "src", "alt", "open", "dir", "lang"]);

function safeUrl(value, image) {
  const v = value.replace(/[\u0000- \u007f-\u009f]/g, "").toLowerCase();
  const scheme = /^([a-z][a-z0-9+.-]*):/.exec(v);
  if (!scheme) return true;  // relative: a media file
  if (["http", "https", "mailto"].includes(scheme[1])) return !image || scheme[1] !== "mailto";
  return image && (scheme[1] === "file" || /^data:image\/(png|jpe?g|gif|webp);/.test(v));
}

function cleanNode(node, into) {
  for (const child of [...node.childNodes]) {
    if (child.nodeType === Node.TEXT_NODE) { into.appendChild(document.createTextNode(child.data)); continue; }
    if (child.nodeType !== Node.ELEMENT_NODE) continue;
    const tag = child.localName;
    if (DROPPED.has(tag) || child.namespaceURI !== "http://www.w3.org/1999/xhtml") continue;
    if (!ALLOWED.has(tag)) { cleanNode(child, into); continue; }
    const el = document.createElement(tag);
    for (const attr of child.attributes) {
      const name = attr.name.toLowerCase();
      if (!ATTRS.has(name)) continue;
      if ((name === "href" || name === "src") && !safeUrl(attr.value, name === "src")) continue;
      el.setAttribute(name, attr.value);
    }
    if (tag === "img") {
      const src = el.getAttribute("src") || "";
      if (src && MEDIA_URL && !/^[a-z][a-z0-9+.-]*:/i.test(src)) el.setAttribute("src", MEDIA_URL + src.split("/").map(p => encodeURIComponent(decoded(p))).join("/"));
      el.setAttribute("loading", "lazy");
    }
    if (tag === "a") el.setAttribute("target", "_blank");
    cleanNode(child, el);
    into.appendChild(el);
  }
}

function sanitize(html) {
  const doc = new DOMParser().parseFromString(`<!doctype html><body>${html}`, "text/html");  // inert
  const out = document.createElement("div");
  cleanNode(doc.body, out);
  return out.innerHTML;
}

// markdown-it percent-encodes ![](...) paths; raw <img src> names aren't.
function decoded(s) { try { return decodeURIComponent(s); } catch (e) { return s; } }

function mediaUrl(name) {
  return /^[a-z][a-z0-9+.-]*:/i.test(name) ? name : MEDIA_URL + encodeURIComponent(name);
}
