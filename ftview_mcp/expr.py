"""Evaluator for FactoryTalk View expressions, used to simulate animation states in previews.

Supports: numbers, "strings", {tag} references, If/Then/Else, AND/OR/NOT (also &&, ||, !),
relational ==, =, <>, !=, <, >, <=, >= and EQ/NE/LT/GT/LE/GE, + - * / MOD, unary minus,
parentheses. Function calls evaluate to 0. Unknown tags take `default`.
"""
from __future__ import annotations

import re

_TOK = re.compile(r"""\s*(?:
    (?P<tag>\{[^{}]*\})
  | (?P<num>\d+\.\d*|\.\d+|\d+)
  | (?P<str>"[^"]*")
  | (?P<op><=|>=|==|<>|!=|&&|\|\||[-+*/()<>=!,])
  | (?P<word>[A-Za-z_][A-Za-z0-9_\\]*)
)""", re.X)

_WORD_OPS = {"AND": "&&", "OR": "||", "NOT": "!", "EQ": "==", "NE": "<>", "LT": "<", "GT": ">", "LE": "<=",
             "GE": ">=", "MOD": "MOD", "IF": "IF", "THEN": "THEN", "ELSE": "ELSE"}


class ExprError(ValueError):
    pass


def tokenize(s: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    s = s.strip()
    while pos < len(s):
        m = _TOK.match(s, pos)
        if not m or m.end() == pos:
            raise ExprError(f"cannot parse expression at {s[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "word" and val.upper() in _WORD_OPS:
            out.append(("op", _WORD_OPS[val.upper()]))
        else:
            out.append((kind, val))
    return out


class _P:
    def __init__(self, toks, values, default):
        self.t, self.i, self.v, self.d = toks, 0, values, default

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self, val=None):
        tok = self.peek()
        if val is not None and tok[1] != val:
            raise ExprError(f"expected {val!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def expr(self):
        if self.peek() == ("op", "IF"):
            self.take()
            c = self.expr()
            self.take("THEN")
            a = self.expr()
            self.take("ELSE")
            b = self.expr()
            return a if _truth(c) else b
        return self.or_()

    def or_(self):
        v = self.and_()
        while self.peek() == ("op", "||"):
            self.take()
            r = self.and_()
            v = 1 if (_truth(v) or _truth(r)) else 0
        return v

    def and_(self):
        v = self.not_()
        while self.peek() == ("op", "&&"):
            self.take()
            r = self.not_()
            v = 1 if (_truth(v) and _truth(r)) else 0
        return v

    def not_(self):
        if self.peek() == ("op", "!"):
            self.take()
            return 0 if _truth(self.not_()) else 1
        return self.cmp()

    def cmp(self):
        v = self.add()
        op = self.peek()[1]
        if op in ("==", "=", "<>", "!=", "<", ">", "<=", ">="):
            self.take()
            r = self.add()
            res = {"==": v == r, "=": v == r, "<>": v != r, "!=": v != r, "<": v < r, ">": v > r,
                   "<=": v <= r, ">=": v >= r}[op]
            return 1 if res else 0
        return v

    def add(self):
        v = self.mul()
        while self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            r = self.mul()
            v = v + r if op == "+" else v - r
        return v

    def mul(self):
        v = self.unary()
        while self.peek()[1] in ("*", "/", "MOD"):
            op = self.take()[1]
            r = self.unary()
            if op == "*":
                v = v * r
            elif op == "/":
                v = v / r if r else 0
            else:
                v = v % r if r else 0
        return v

    def unary(self):
        if self.peek() == ("op", "-"):
            self.take()
            return -self.unary()
        return self.primary()

    def primary(self):
        kind, val = self.take()
        if kind == "num":
            return float(val)
        if kind == "str":
            return val[1:-1]
        if kind == "tag":
            key = val[1:-1].strip()
            return self.v.get(key, self.v.get(_bare(key), self.d))
        if kind == "op" and val == "(":
            v = self.expr()
            self.take(")")
            return v
        if kind == "word":
            if self.peek() == ("op", "("):  # function call -> 0
                depth = 0
                while True:
                    k, x = self.take()
                    if k is None:
                        raise ExprError("unbalanced parentheses in function call")
                    depth += x == "("
                    depth -= x == ")"
                    if depth == 0:
                        break
                return 0
            return self.v.get(val, self.d)  # bare HMI tag name
        raise ExprError(f"unexpected token {val!r}")


def _truth(v) -> bool:
    return bool(v) if not isinstance(v, str) else bool(v)


def _bare(tag: str) -> str:
    """'[SCADA]P101_RUN' -> 'P101_RUN' so callers can key values by plain tag names."""
    return re.sub(r"^(?:[^\[]*::)?\[[^\]]+\]", "", tag)


def evaluate(expression: str, values: dict | None = None, default=0):
    toks = tokenize(expression)
    if not toks:
        return default
    vals = dict(values or {})
    for k, v in list(vals.items()):  # let '[SC]Tag' keys also match bare {Tag} references
        vals.setdefault(_bare(k), v)
    p = _P(toks, vals, default)
    v = p.expr()
    if p.i != len(toks):
        raise ExprError(f"unexpected {toks[p.i][1]!r}")
    return v
