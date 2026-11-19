"""Numerical analysis for the calculator: definite integrals and the roots of an equation.

Both take a plain float function `f(x)`; the calculator builds it from an expression. A function
that is undefined somewhere returns None there (for roots) or raises (for integrals), so a bad
point is reported rather than skipped silently.
"""

from __future__ import annotations

import heapq
import math
from typing import Callable

# 15-point Gauss–Kronrod rule with its embedded 7-point Gauss rule, on [-1, 1].
_XK = (0.991455371120812639206854697526329, 0.949107912342758524526189684047851, 0.864864423359769072789712788640926,
       0.741531185599394439863864773280788, 0.586087235467691130294144845693013, 0.405845151377397166906606412076961,
       0.207784955007898467600689403773245, 0.0)
_WK = (0.022935322010529224963732008058970, 0.063092092629978553290700663189204, 0.104790010322250183839876322541518,
       0.140653259715525918745189590510238, 0.169004726639267902826583426598550, 0.190350578064785409913256402421014,
       0.204432940075298892414161999234649, 0.209482141084727828012999174891714)
_WG = (0.129484966168869693270611432679082, 0.279705391489276667901467771423780, 0.381830050505118944950369775488975,
       0.417959183673469387755102040816327)

MAX_INTERVALS = 300
MAX_ROOTS = 12


class AnalysisError(Exception):
    pass


def _gk15(f: Callable[[float], float], a: float, b: float) -> tuple[float, float]:
    c, h = (a + b) / 2, (b - a) / 2
    fc = f(c)
    kronrod, gauss = fc * _WK[7], fc * _WG[3]
    for j in range(7):
        dx = h * _XK[j]
        pair = f(c - dx) + f(c + dx)
        kronrod += _WK[j] * pair
        if j % 2 == 1:
            gauss += _WG[j // 2] * pair
    return kronrod * h, abs((kronrod - gauss) * h)


def integrate(f: Callable[[float], float], a: float, b: float) -> float:
    """∫ₐᵇ f(x) dx by adaptive Gauss–Kronrod; either limit may be infinite."""
    if a == b:
        return 0.0
    if a > b:
        return -integrate(f, b, a)
    g, lo, hi = f, a, b
    # An infinite limit is mapped to the end of a finite range; a point that rounds onto that end
    # (after many halvings) stands for infinity, where an integrand that converges vanishes.
    if math.isinf(a) and math.isinf(b):  # x = t / (1 − t²)
        g, lo, hi = (lambda t: 0.0 if abs(t) == 1 else f(t / (1 - t * t)) * (1 + t * t) / (1 - t * t) ** 2), -1.0, 1.0
    elif math.isinf(b):  # x = a + t / (1 − t)
        g, lo, hi = (lambda t: 0.0 if t == 1 else f(a + t / (1 - t)) / (1 - t) ** 2), 0.0, 1.0
    elif math.isinf(a):  # x = b − (1 − t) / t
        g, lo, hi = (lambda t: 0.0 if t == 0 else f(b - (1 - t) / t) / (t * t)), 0.0, 1.0

    def checked(x: float) -> float:
        y = g(x)
        if not math.isfinite(y):
            raise AnalysisError("The integrand is infinite inside the limits")
        return y

    value, err = _gk15(checked, lo, hi)
    heap = [(-err, lo, hi, value)]
    total, total_err = value, err
    while total_err > max(1e-13, 1e-11 * abs(total)) and len(heap) < MAX_INTERVALS:
        neg_err, x0, x1, v = heapq.heappop(heap)
        mid = (x0 + x1) / 2
        if mid in (x0, x1):  # can't split any finer
            heapq.heappush(heap, (neg_err, x0, x1, v))
            break
        v1, e1 = _gk15(checked, x0, mid)
        v2, e2 = _gk15(checked, mid, x1)
        total += v1 + v2 - v
        total_err += e1 + e2 + neg_err
        heapq.heappush(heap, (-e1, x0, mid, v1))
        heapq.heappush(heap, (-e2, mid, x1, v2))
    total = math.fsum(item[3] for item in heap)
    total_err = sum(-item[0] for item in heap)
    # Splitting toward a point where the integral diverges keeps adding about as much each time,
    # while toward a singularity it survives (1/√x at 0) each piece adds less and less.
    tiny = math.fsum(abs(item[3]) for item in heap if item[2] - item[1] < 1e-9 * (hi - lo))
    piles_up = tiny > 1e-3 * abs(total)
    if total_err > 1e-6 * max(1.0, abs(total)) or piles_up:
        raise AnalysisError("The integral doesn't settle on a value (it may diverge)")
    return total


# -- roots ------------------------------------------------------------------------------------
def _grid(lo: float | None, hi: float | None) -> list[float]:
    if lo is not None and hi is not None:
        n = 4000
        return [lo + (hi - lo) * i / n for i in range(n + 1)]
    near = [i / 100 for i in range(-1000, 1001)]  # −10 … 10
    far = [10 * 10 ** (5 * i / 300) for i in range(1, 301)]  # 10 … 10⁶
    return sorted({*near, *far, *(-x for x in far)})


def _bisect(g: Callable[[float], float | None], a: float, b: float, ga: float) -> float | None:
    for _ in range(200):
        m = (a + b) / 2
        if m in (a, b):
            break
        gm = g(m)
        if gm is None:
            return None
        if gm == 0:
            return m
        if (gm < 0) == (ga < 0):
            a, ga = m, gm
        else:
            b = m
    return (a + b) / 2


def _golden_min(g: Callable[[float], float | None], a: float, b: float) -> float:
    """Where |g| is smallest in [a, b] (for a root the curve only touches)."""
    r = (math.sqrt(5) - 1) / 2

    def size(x):
        v = g(x)
        return math.inf if v is None else abs(v)

    c, d = b - r * (b - a), a + r * (b - a)
    fc, fd = size(c), size(d)
    for _ in range(120):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - r * (b - a)
            fc = size(c)
        else:
            a, c, fc = c, d, fd
            d = a + r * (b - a)
            fd = size(d)
        if b - a < 1e-15 * max(1.0, abs(a)):
            break
    return (a + b) / 2


def _accept(g, x: float, scale: float, tol: float = 1e-8) -> bool:
    v = g(x)
    return v is not None and abs(v) <= tol * max(1.0, scale)


def find_roots(g: Callable[[float], float | None], lo: float | None = None,
               hi: float | None = None) -> tuple[list[float], bool]:
    """Real roots of g between lo and hi (default: a search out to ±10⁶, densest near zero), nearest
    zero first when there are more than MAX_ROOTS. Returns (roots in increasing order, whether more
    were found than are listed)."""
    xs = _grid(lo, hi)
    ys = [g(x) for x in xs]
    found: list[float] = []
    for i in range(len(xs) - 1):
        a, b, ga, gb = xs[i], xs[i + 1], ys[i], ys[i + 1]
        if ga is None or gb is None:
            continue
        if ga == 0:
            found.append(a)
        elif gb != 0 and (ga < 0) != (gb < 0):
            r = _bisect(g, a, b, ga)
            if r is not None and _accept(g, r, max(abs(ga), abs(gb))):  # a pole changes sign too
                found.append(r)
    if ys[-1] == 0:
        found.append(xs[-1])
    for i in range(1, len(xs) - 1):  # roots where the curve touches zero without crossing
        ga, gm, gb = ys[i - 1], ys[i], ys[i + 1]
        if None in (ga, gm, gb) or gm == 0 or (ga < 0) != (gm < 0) or (gm < 0) != (gb < 0):
            continue
        if abs(gm) <= abs(ga) and abs(gm) <= abs(gb):
            r = _golden_min(g, xs[i - 1], xs[i + 1])
            if _accept(g, r, max(abs(ga), abs(gb)), 1e-10):
                found.append(r)
    roots: list[float] = []
    for r in sorted(found):
        if roots and abs(r - roots[-1]) <= 1e-7 * max(1.0, abs(r)):
            continue
        roots.append(r)
    more = len(roots) > MAX_ROOTS
    if more:
        roots = sorted(sorted(roots, key=abs)[:MAX_ROOTS])
    return roots, more


def root_near(g: Callable[[float], float | None], guess: float) -> float | None:
    """The root nearest a guess: Newton's method (numerical slope), else the nearest sign change."""
    x = guess
    for _ in range(60):
        v = g(x)
        if v is None:
            break
        if v == 0:
            return x
        h = 1e-7 * max(1.0, abs(x))
        up, down = g(x + h), g(x - h)
        if up is None or down is None or up == down:
            break
        step = v * 2 * h / (up - down)
        x -= step
        if not math.isfinite(x):
            break
        if abs(step) <= 1e-14 * max(1.0, abs(x)):
            return x if _accept(g, x, abs(v) + 1, 1e-8) else None
    width = 1e-3 * max(1.0, abs(guess))
    gv = g(guess)
    while width < 1e7 * max(1.0, abs(guess)):  # outward until the sign changes
        for side in (guess - width, guess + width):
            gs = g(side)
            if gs is not None and gv is not None and gs != 0 and (gs < 0) != (gv < 0):
                a, b = sorted((guess, side))
                r = _bisect(g, a, b, gv if a == guess else gs)
                if r is not None and _accept(g, r, max(abs(gs), abs(gv))):
                    return r
            elif gs == 0:
                return side
        width *= 2
    return None
