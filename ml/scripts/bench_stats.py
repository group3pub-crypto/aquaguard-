"""Statistics helpers for ``ml/scripts/benchmark.py`` (pure numpy/scipy, no I/O).

Everything works on *unit-level* sufficient statistics (per flat or per episode) so a bootstrap
resample is a single fancy-index + sum, and two policies can be resampled with the SAME indices
(paired bootstrap).
"""

from __future__ import annotations

import numpy as np
from scipy.stats import binomtest

CLASSES = ["normal", "leak", "burst", "overuse"]
UNKNOWN = 4  # a flag with no class (IsolationForest-only alert): never equals a true class
IDX = {c: i for i, c in enumerate(CLASSES)} | {"unknown": UNKNOWN}


def to_idx(labels: np.ndarray) -> np.ndarray:
    return np.fromiter((IDX[str(v)] for v in labels), dtype=np.int8, count=len(labels))


# ----------------------------------------------------------------------------- window metrics
def class_counts(y: np.ndarray, p: np.ndarray) -> np.ndarray:
    """(4, 3) array of [tp, fp, fn] per true class. ``p`` may hold UNKNOWN (counts as a miss)."""
    out = np.zeros((len(CLASSES), 3))
    for c in range(len(CLASSES)):
        out[c, 0] = np.sum((y == c) & (p == c))
        out[c, 1] = np.sum((y != c) & (p == c))
        out[c, 2] = np.sum((y == c) & (p != c))
    return out


def binary_counts(y: np.ndarray, p: np.ndarray) -> np.ndarray:
    """[tp, fp, fn] for 'anything non-normal' vs 'normal' (class-agnostic alert quality)."""
    yt, pt = y != 0, p != 0
    return np.array([np.sum(yt & pt), np.sum(~yt & pt), np.sum(yt & ~pt)], dtype=float)


def prf(counts: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """P, R, F1 from [..., 3] counts of [tp, fp, fn] (zero-safe)."""
    tp, fp, fn = counts[..., 0], counts[..., 1], counts[..., 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        pr = np.where(tp + fp > 0, tp / (tp + fp), 0.0)
        rc = np.where(tp + fn > 0, tp / (tp + fn), 0.0)
        f1 = np.where(pr + rc > 0, 2 * pr * rc / (pr + rc), 0.0)
    return pr, rc, f1


def macro_f1(counts: np.ndarray) -> np.ndarray:
    """Macro-F1 over classes that have support. ``counts`` is [..., 4, 3]."""
    _, _, f1 = prf(counts)
    present = (counts[..., 0] + counts[..., 2]) > 0
    n = np.maximum(present.sum(axis=-1), 1)
    return (f1 * present).sum(axis=-1) / n


def binary_f1(counts: np.ndarray) -> np.ndarray:
    return prf(counts)[2]


# ----------------------------------------------------------------------------- bootstrap
def boot_idx(n: int, b: int, rng: np.random.Generator) -> np.ndarray:
    return rng.integers(0, n, size=(b, n))


def pct_ci(x: np.ndarray, lo: float = 2.5, hi: float = 97.5) -> tuple[float, float]:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return (float("nan"), float("nan"))
    return float(np.percentile(x, lo)), float(np.percentile(x, hi))


def boot_pvalue(diff: np.ndarray) -> float:
    """Two-sided bootstrap p-value for 'difference == 0' from the resampled differences."""
    d = np.asarray(diff, dtype=float)
    d = d[np.isfinite(d)]
    if d.size == 0 or np.all(d == 0):
        return 1.0
    b = d.size
    return float(min(1.0, 2 * min((np.sum(d <= 0) + 1) / (b + 1), (np.sum(d >= 0) + 1) / (b + 1))))


def holm(pvals: list[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values (same order as input)."""
    m = len(pvals)
    order = np.argsort(pvals)
    adj = np.empty(m)
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * pvals[i]))
        adj[i] = run
    return adj.tolist()


def mcnemar_exact(model_ok: np.ndarray, base_ok: np.ndarray) -> dict:
    """Exact (binomial) McNemar test on paired correct/incorrect outcomes."""
    m, bs = np.asarray(model_ok, bool), np.asarray(base_ok, bool)
    b = int(np.sum(m & ~bs))  # model right, baseline wrong
    c = int(np.sum(~m & bs))  # model wrong, baseline right
    p = 1.0 if b + c == 0 else float(binomtest(min(b, c), b + c, 0.5).pvalue)
    return {"model_only": b, "baseline_only": c, "p": p}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return float(max(0.0, centre - half)), float(min(1.0, centre + half))


def verdict(adj_p: float, diff: float, higher_better: bool, alpha: float = 0.05) -> str:
    """'better' / 'worse' only if the Holm-adjusted p < alpha, else 'not demonstrated'."""
    if not np.isfinite(diff) or adj_p >= alpha or diff == 0:
        return "not demonstrated"
    good = diff > 0 if higher_better else diff < 0
    return "better" if good else "worse"
