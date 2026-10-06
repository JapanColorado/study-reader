// Markdown with $math$ (KaTeX), shared by transcript.html and notes.html.
// Needs katex and markdown-it loaded first.

const md = markdownit({ html: false, linkify: true });

const MATH = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$(?!\s)([^$\n]+?)(?<!\s)\$(?!\d)/g;
const CODE = /(```[\s\S]*?(?:```|$)|`[^`\n]*`)/;

function esc(s) { return s.replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

// Math is lifted out before Markdown runs so underscores and backslashes survive.
function renderMarkdown(src, inline = false) {
  const store = [];
  const parts = src.split(CODE).map((part, i) => {
    if (i % 2) return part;
    return part.replace(MATH, (m, d1, d2, i1, i2) => {
      const display = d1 !== undefined || d2 !== undefined;
      store.push({ tex: d1 ?? d2 ?? i1 ?? i2, display });
      return `@@MATH${store.length - 1}@@`;
    });
  });
  return (inline ? md.renderInline : md.render).call(md, parts.join("")).replace(/@@MATH(\d+)@@/g, (m, n) => {
    const { tex, display } = store[+n];
    try { return katex.renderToString(tex, { displayMode: display, throwOnError: false }); }
    catch (e) { return esc(tex); }
  });
}
