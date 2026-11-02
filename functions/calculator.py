"""A safe, deterministic calculator — no AI involved, ever.

Parses the everyday things people type at a calculator:

    25% of 800            234 * 56              (3 + 4) * 2^10
    100 + 15%             -40 / 2.5             sqrt(144) + ln(e^2)
    1,000,000 / 3         2**8                  15 % of 500 + 10

Safety: the expression is parsed with :mod:`ast` and evaluated against a
strict whitelist of node types — no names, calls or attributes other
than the ones below can ever evaluate.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from dataclasses import dataclass

from functions.helpers import ParadiseError

_FUNCTIONS = {
    "sqrt": math.sqrt,
    "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
    "abs": abs,
    "floor": math.floor,
    "ceil": math.ceil,
    "round": lambda x, n=0: round(x, int(n)),
    "ln": math.log,
    "log": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "mod": operator.mod,
}

_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "π": math.pi}

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_MAX_DIGITS = 400  # refuse absurd exponent results before they compute

# Number written with optional thousand separators / decimals.
_NUMBER = r"(?:\d[\d,]*(?:\.\d+)?|\.\d+)"


@dataclass(slots=True)
class CalcResult:
    expression: str
    result: float
    note: str = ""

    @property
    def result_text(self) -> str:
        return _format_number(self.result)

    @property
    def display(self) -> str:
        base = f"{self.expression} = **{self.result_text}**"
        return f"{base}\n-# {self.note}" if self.note else base


def _format_number(value: float) -> str:
    if isinstance(value, bool):  # pragma: no cover - defensive
        return str(value)
    if math.isnan(value) or math.isinf(value):
        raise ParadiseError("that result isn't a real number.")
    if float(value).is_integer() and abs(value) < 1e16:
        return f"{int(value):,}"
    text = f"{value:,.8f}".rstrip("0").rstrip(".")
    return text


# ---------------------------------------------------------------------------
# Text → expression normalisation
# ---------------------------------------------------------------------------

_WORD_OPS = [
    (re.compile(r"\bmultiplied by\b", re.I), "*"),
    (re.compile(r"\bdivided by\b", re.I), "/"),
    (re.compile(r"\bplus\b", re.I), "+"),
    (re.compile(r"\bminus\b", re.I), "-"),
    (re.compile(r"\btimes\b", re.I), "*"),
    (re.compile(r"\bover\b", re.I), "/"),
    (re.compile(r"\bpercent(?:age)?\b", re.I), "%"),
    (re.compile(r"\bsquared\b", re.I), "** 2"),
    (re.compile(r"\bcubed\b", re.I), "** 3"),
    (re.compile(r"\bsquare root of\b", re.I), "sqrt "),
    (re.compile(r"\braised to(?: the power of)?\b", re.I), "**"),
    (re.compile(r"\bto the power of\b", re.I), "**"),
]

# "N% of X" — X ends at an unnested + or - ("15% of 500 + 10" = 75 + 10).
_PERCENT_OF = re.compile(
    rf"(?P<pct>{_NUMBER})\s*%\s*of\s*(?P<rest>(?:\([^()]*\)|[^()+\-])+)", re.IGNORECASE
)
_PERCENT_STANDALONE = re.compile(rf"^\s*(?P<pct>{_NUMBER})\s*%\s*$")
# "800 + 25%" → add 25% of the left operand; "800 - 25%" likewise.
_PERCENT_AFTER_OP = re.compile(
    rf"(?P<base>[^()+\-*/].*?)\s*(?P<op>[+\-])\s*(?P<pct>{_NUMBER})\s*%\s*$"
)
# "a mod b" spelled out, or "a % b" clearly spaced between bare numbers.
_MOD_WORD = re.compile(
    rf"(?P<a>{_NUMBER})\s+mod(?:ulo)?\s+(?P<b>{_NUMBER})", re.IGNORECASE
)
_MOD_SPACED = re.compile(rf"(?P<a>{_NUMBER})\s+%\s+(?P<b>{_NUMBER})")
_NUMBER_PERCENT = re.compile(rf"(?P<num>{_NUMBER})\s*%")
_CALLABLE = "|".join(sorted(k for k in _FUNCTIONS if k != "mod"))


def _clean_value(text: str) -> float:
    try:
        return float(text.replace(",", "").strip())
    except ValueError:
        raise ParadiseError(f"I couldn't read the number `{text}`.") from None


def _normalize_expression(text: str) -> str:
    expr = text.strip()
    expr = expr.replace("×", "*").replace("✕", "*").replace("÷", "/")
    expr = expr.replace("−", "-").replace("–", "-").replace("—", "-")
    expr = expr.replace("(", " ( ").replace(")", " ) ")
    expr = expr.replace("^", "**")
    # Function-ish words first, then operator words.
    for pattern, replacement in _WORD_OPS:
        expr = pattern.sub(f" {replacement} ", expr)
    # Thousand separators: "1,000,000" → "1000000" (only exact 3-digit groups).
    expr = re.sub(
        r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b",
        lambda m: m.group(0).replace(",", ""),
        expr,
    )
    expr = re.sub(r"\s+", " ", expr).strip()
    # "sqrt 144" → "sqrt(144)"; "sqrt(144)" stays.
    expr = re.sub(
        rf"\b({_CALLABLE})\s+({_NUMBER})\b",
        lambda m: f"{m.group(1)}({m.group(2)})",
        expr,
    )
    return expr


def _flatten_percent_of(expr: str) -> str:
    """Expand the everyday percent idioms before AST evaluation.

    Contract (documented in behaviour, matched to real calculators):
      * ``N% of X`` → ``(N/100) * X``
      * ``N%``      → ``N/100``
      * ``a + N%`` / ``a - N%`` (bare number on the left) → ``a ± a·N/100``
      * ``a % b`` / ``a mod b`` clearly spaced between numbers → modulo
    """
    expr = expr.strip().rstrip("=?.").strip()

    standalone = _PERCENT_STANDALONE.match(expr)
    if standalone:
        return f"({standalone.group('pct').replace(',', '')}/100)"

    match = _PERCENT_OF.search(expr)
    if match:
        pct = match.group("pct").replace(",", "")
        inner = f"(({pct}/100) * ({_flatten_percent_of(match.group('rest').strip())}))"
        prefix = expr[: match.start()].strip()
        tail = expr[match.end() :].strip()
        if prefix:
            prefix = _flatten_percent_of(prefix)
        if tail:
            tail = _flatten_percent_of(tail)
        return " ".join(part for part in (prefix, inner, tail) if part)

    trailing = _PERCENT_AFTER_OP.match(expr)
    if trailing:
        base = trailing.group("base").strip()
        op = trailing.group("op")
        pct = trailing.group("pct").replace(",", "")
        # Only apply the calculator convention when the left side is a bare
        # number (“a*b + 25%” would be ambiguous beyond the common case).
        if re.fullmatch(_NUMBER, base.replace(" ", "")):
            return f"({base} {op} ({base} * {pct}/100))"

    # Modulo between bare numbers stays a modulo; protect it with a marker
    # while the remaining "N%" percent-shorthand is expanded.
    expr = _MOD_WORD.sub(lambda m: f"{m.group('a')} ⟁ {m.group('b')}", expr)
    expr = _MOD_SPACED.sub(lambda m: f"{m.group('a')} ⟁ {m.group('b')}", expr)
    expr = _NUMBER_PERCENT.sub(
        lambda m: f"({m.group('num').replace(',', '')}/100)", expr
    )
    return expr.replace("⟁", "%")


# ---------------------------------------------------------------------------
# Safe AST evaluation
# ---------------------------------------------------------------------------


def _eval_node(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ParadiseError("I can only compute numbers.")
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return float(_CONSTANTS[node.id])
        raise ParadiseError(f"unknown name `{node.id}` — try numbers and operators.")
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BINOPS:
            raise ParadiseError("that operator isn't supported.")
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if op_type in (ast.Div, ast.FloorDiv, ast.Mod) and right == 0:
            raise ParadiseError("division by zero — not today.")
        if op_type is ast.Pow:
            if abs(right) > 10_000 or (abs(left) > 1 and right * math.log10(max(abs(left), 1)) > _MAX_DIGITS):
                raise ParadiseError("that number would be far too big to print.")
            if left == 0 and right < 0:
                raise ParadiseError("can't raise zero to a negative power.")
        result = _BINOPS[op_type](left, right)
        return float(result)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return float(_UNARY[type(node.op)](_eval_node(node.operand)))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise ParadiseError("that function isn't supported.")
        if node.keywords:
            raise ParadiseError("keep it to plain math, please.")
        if len(node.args) > 2:
            raise ParadiseError("that function takes at most two numbers.")
        args = [_eval_node(arg) for arg in node.args]
        try:
            return float(_FUNCTIONS[node.func.id](*args))
        except ZeroDivisionError:
            raise ParadiseError("division by zero — not today.") from None
        except (ValueError, OverflowError):
            raise ParadiseError("that doesn't compute in real numbers.") from None
    raise ParadiseError("that looks less like math than I can handle.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_STATS_SHAPE = re.compile(
    r"^(?:the\s+)?(?P<op>average|mean|sum|median|minimum|min|maximum|max|product)\s+of\s+(?P<rest>.+)$",
    re.IGNORECASE,
)

_EQUATION_VARS = re.compile(r"(?<![a-z_])(?P<var>[a-z])(?![a-z_])", re.IGNORECASE)


def _stats(op: str, rest: str) -> CalcResult | None:
    """'average of 4 8 15' → the named statistic of a number list."""
    numbers: list[float] = []
    for token in re.split(r"[,;\s]+", rest.strip()):
        if not token or token.casefold() in ("and", "&"):
            continue
        if not _looks_numeric(token):
            return None  # not a pure number list — not our problem
        numbers.append(float(token.replace(",", ".").rstrip(".,")
                             if token.count(".") == 0 else token))
    if len(numbers) < 2:
        return None
    op = op.casefold()
    if op in ("average", "mean"):
        value = sum(numbers) / len(numbers)
    elif op == "sum":
        value = sum(numbers)
    elif op == "median":
        ordered = sorted(numbers)
        mid = len(ordered) // 2
        value = (
            ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        )
    elif op in ("minimum", "min"):
        value = min(numbers)
    elif op in ("maximum", "max"):
        value = max(numbers)
    elif op == "product":
        value = 1.0
        for number in numbers:
            value *= number
    else:  # pragma: no cover - the regex only admits the above
        return None
    label = {"average": "mean", "mean": "mean"}.get(op, op)
    return CalcResult(expression=f"{label} of {len(numbers)} numbers", result=value)


def _looks_numeric(token: str) -> bool:
    try:
        float(token.replace(",", "."))
    except ValueError:
        return False
    return True


def _solve_equation(text: str) -> CalcResult | None:
    """Solve a single-variable linear equation: '2x + 3 = 11' → x = 4.

    Both sides are evaluated twice (variable → 0, then → 1) with the same
    safe expression evaluator used everywhere else, which turns each side
    into ``a·x + b`` without any new parsing risk. No solution, infinite
    solutions and non-linear shapes are reported honestly.
    """
    if "=" not in text or len(text) > 200:
        return None
    left_raw, _, right_raw = text.partition("=")
    if not left_raw.strip() or not right_raw.strip():
        return None
    variables = {match.group("var").casefold() for match in _EQUATION_VARS.finditer(text)}
    if len(variables) != 1:
        return None
    var = variables.pop()

    def _side(value: float, side: str) -> float | None:
        substituted = _EQUATION_VARS.sub(
            lambda m: f"({value})" if m.group("var").casefold() == var else m.group(0),
            side,
        )
        # "2x" / "3(x+1)" style implicit multiplication → explicit.
        substituted = re.sub(r"(\d)\s*([a-z(])", r"\1*\2", substituted, flags=re.IGNORECASE)
        try:
            normalised = _normalize_expression(substituted)
            tree = ast.parse(_flatten_percent_of(normalised), mode="eval")
            return _eval_node(tree)
        except (SyntaxError, ParadiseError, ZeroDivisionError, ValueError):
            return None

    samples: list[tuple[float, float]] = []
    for side in (left_raw, right_raw):
        at_zero = _side(0, side)
        at_one = _side(1, side)
        at_two = _side(2, side)
        if at_zero is None or at_one is None or at_two is None:
            return None
        # A straight line or not? f(2) - f(0) must equal 2·(f(1) - f(0)).
        slope = at_one - at_zero
        if abs((at_two - at_zero) - 2 * slope) > 1e-9 * max(1.0, abs(at_two)):
            return None  # non-linear — not pretending we solved it
        samples.append((slope, at_zero))
    (a1, b1), (a2, b2) = samples
    if abs(a1 - a2) < 1e-12:
        if abs(b1 - b2) < 1e-12:
            raise ParadiseError("that equation is true for *every* value — infinite solutions.")
        raise ParadiseError("those two sides never meet — no solution.")
    value = (b2 - b1) / (a1 - a2)
    return CalcResult(expression=f"{text} → {var}", result=value)


def calculate(raw: str) -> CalcResult:
    """Evaluate a math query, raising a friendly ``ParadiseError`` on failure."""
    text = raw.strip()
    if not text:
        raise ParadiseError("give me something to compute, e.g. `25% of 800`.")
    if len(text) > 300:
        raise ParadiseError("that's a bit long for a calculator query.")

    stats_match = _STATS_SHAPE.match(strip_question_words(text))
    if stats_match is not None:
        result = _stats(stats_match.group("op"), stats_match.group("rest"))
        if result is not None:
            return result
    equation = _solve_equation(strip_question_words(text).rstrip("?!.").strip())
    if equation is not None:
        return equation

    normalised = _normalize_expression(text)
    flattened = _flatten_percent_of(normalised)

    try:
        tree = ast.parse(flattened, mode="eval")
    except SyntaxError:
        raise ParadiseError(
            f"I couldn't parse that as math: `{text}`\n"
            "try things like `25% of 800`, `(3 + 4) * 2^10`, `sqrt(144)`."
        ) from None

    value = _eval_node(tree)
    return CalcResult(expression=_display_expression(text, normalised), result=value)


def _display_expression(original: str, normalised: str) -> str:
    """What to echo back next to the result.

    The user's own notation reads best ("15% of 847 = 127.05", "7283 × 492
    = 3,583,236"); the internal flattened form ("((15/100) * (847))") is an
    implementation detail nobody asked to see. Falls back to the normalised
    expression if the input was only punctuation.
    """
    shown = " ".join(str(original).split()).strip()
    return shown or " ".join(str(normalised).split()).strip()


# ---------------------------------------------------------------------------
# Router helpers
# ---------------------------------------------------------------------------

_QUESTION_LEAD = re.compile(
    r"^(?:(?:hey(?:\s+paradise)?|please|pls|ok(?:ay)?|so)\s*[,.]?\s*)*"
    r"(?:what(?:'s| is|s)?|whats|how much is|calculate|compute|evaluate|solve|maths?|tell me)\s+",
    re.IGNORECASE,
)

_PURE_MATH_SHAPE = re.compile(
    r"^\s*[-+()0-9.,\sπa-z]*(?:[+\-*/%×÷^()]|\d\s*[+\-*/%×÷^]|\^\d)", re.IGNORECASE
)


def strip_question_words(text: str) -> str:
    """Remove conversational lead-ins: 'what is 2+2' → '2+2'."""
    return _QUESTION_LEAD.sub("", text.strip()).strip()


def _stats_list_shape(rest: str) -> bool:
    """True when *rest* is a plain list of numbers ('4 8 15 16', '3, 5, and 7')."""
    tokens = [t for t in re.split(r"[,;\s]+", rest.strip()) if t]
    numbers = [t for t in tokens if t.casefold() not in ("and", "&")]
    if len(numbers) < 2 or len(numbers) != len(tokens) - (len(tokens) - len(numbers)):
        return False
    return all(_looks_numeric(t) for t in numbers)


def _equation_shape(probe: str) -> bool:
    """True for single-variable linear shapes like '2x + 3 = 11'."""
    if probe.count("=") != 1 or len(probe) > 100:
        return False
    variables = {m.group("var").casefold() for m in _EQUATION_VARS.finditer(probe)}
    if len(variables) != 1:
        return False
    var = variables.pop()
    sides = []
    for side in probe.split("="):
        if not side.strip() or not re.search(r"\d", side):
            return False
        substituted = _EQUATION_VARS.sub(
            lambda m: "(1)" if m.group("var").casefold() == var else m.group(0), side
        )
        substituted = re.sub(r"(\d)\s*([a-z(])", r"\1*\2", substituted, flags=re.IGNORECASE)
        if not re.fullmatch(r"[\d\s+\-*/%×÷^().,]+", substituted):
            return False
        sides.append(substituted)
    return True


def looks_like_math(text: str) -> bool:
    """True when the text is shaped like a calculation request."""
    probe = strip_question_words(text).rstrip("?!.").strip()
    if not probe or len(probe) > 200 or "\n" in probe:
        return False
    if _PERCENT_OF.search(probe) or _PERCENT_STANDALONE.match(probe):
        return True
    stats = _STATS_SHAPE.match(probe)
    if stats is not None and _stats_list_shape(stats.group("rest")):
        return True
    if _equation_shape(probe):
        return True
    # Must contain a digit and an operator-ish character, and no letters
    # outside the known function/constant/word-op vocabulary.
    if not re.search(r"\d", probe):
        return False
    if not _PURE_MATH_SHAPE.match(probe):
        return False
    scrubbed = probe
    for pattern, _ in _WORD_OPS:
        scrubbed = pattern.sub(" ", scrubbed)
    scrubbed = re.sub(r"\bmod(?:ulo)?\b", " ", scrubbed, flags=re.IGNORECASE)
    scrubbed = re.sub(rf"\b({'|'.join(sorted(_FUNCTIONS | _CONSTANTS))})\b", " ", scrubbed)
    # Anything left that is alphabetic means it's prose, not math.
    if re.search(r"[a-zA-Z]", scrubbed) and not _PERCENT_OF.search(probe):
        return False
    return any(op in probe for op in "+-*/×÷^%(")


def extract_expression(text: str) -> str | None:
    """Return the fragment of *text* that actually evaluates, or ``None``.

    People wrap arithmetic in conversation ("bro calculate 29% of 734",
    "ok so whats 25 * 32 please"). The router needs the part the
    calculator can parse, and the only trustworthy test for that is the
    calculator itself — so this trims leading words until :func:`calculate`
    accepts the remainder. Pure, bounded (at most a handful of attempts)
    and side-effect free.
    """
    cleaned = strip_question_words(" ".join(str(text or "").split())).strip(" ?!.")
    if not cleaned or len(cleaned) > 200:
        return None
    words = cleaned.split()
    for start in range(min(len(words), 6)):
        candidate = " ".join(words[start:]).strip(" ?!.")
        if not candidate or not any(char.isdigit() for char in candidate):
            break
        try:
            calculate(candidate)
        except ParadiseError:
            continue
        except Exception:  # noqa: BLE001 - never let probing raise
            continue
        return candidate
    return None
