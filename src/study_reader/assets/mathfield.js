// MathLive field setup shared by the calculator and quiz answers.
MathfieldElement.soundsDirectory = null;
MathfieldElement.plonkSound = null;

// Desmos-like typing: names turn into functions and symbols, and nothing exotic (\in, \times for xx, …).
const op = name => `\\operatorname{${name}}`;
const MATH_SHORTCUTS = {
  pi: "\\pi", tau: "\\tau", inf: "\\infty", infty: "\\infty", infinity: "\\infty",
  sqrt: "\\sqrt{#?}", cbrt: "\\sqrt[3]{#?}", nthroot: "\\sqrt[#?]{#?}", frac: "\\frac{#?}{#?}",
  abs: "\\left|#?\\right|", floor: "\\left\\lfloor#?\\right\\rfloor", ceil: "\\left\\lceil#?\\right\\rceil",
  sin: "\\sin", cos: "\\cos", tan: "\\tan", sinh: "\\sinh", cosh: "\\cosh", tanh: "\\tanh",
  asin: "\\arcsin", acos: "\\arccos", atan: "\\arctan", arcsin: "\\arcsin", arccos: "\\arccos", arctan: "\\arctan",
  asinh: op("asinh"), acosh: op("acosh"), atanh: op("atanh"), atan2: op("atan2"),
  ln: "\\ln", log: "\\log", log10: op("log10"), log2: op("log2"), exp: "\\exp", deg: "\\deg", rad: op("rad"),
  min: "\\min", max: "\\max", gcd: "\\gcd", lcm: op("lcm"), comb: op("comb"), perm: op("perm"),
  hypot: op("hypot"), round: op("round"), trunc: op("trunc"), factorial: op("factorial"), mod: "\\bmod",
  "*": "\\cdot",
};
function configureMathField(mf) {
  mf.inlineShortcuts = MATH_SHORTCUTS;
  mf.smartSuperscript = false;  // stay in the exponent until → (2^10, not 2^1 then 0)
  mf.mathModeSpace = "\\,";
  mf.menuItems = [];
}
