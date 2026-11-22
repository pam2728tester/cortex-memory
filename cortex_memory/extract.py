"""Deterministic, high-precision fact extraction (no LLM, no API key).

Design rules (from the Oracle design review, 2026-11-22):
  * Precision over recall. A few accurate memories beat many speculative ones.
  * Role gating: only the USER's own words become preferences/decisions/stack
    facts. Command success/failure is read from TOOL RESULTS, never from prose.
  * Whole sentences are stored, so negation ("we decided NOT to use Redis")
    keeps its meaning instead of being inverted by a clipped capture group.
  * Questions and hypotheticals ("if we decide...", "should we use...") never
    produce facts.
  * Anything that looks like a secret is dropped, not redacted-and-kept.

Facts map onto the existing (subject, predicate, object) schema:
  single-valued  predicate = "uses:database"        -> a new value SUPERSEDES the old
  multi-valued   predicate = "decision:<hash>"      -> distinct items coexist,
                                                       an identical one REINFORCES

An optional LLM extractor can be plugged in with `register_llm_extractor`; its
output goes through the same validation as the deterministic candidates.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable

from .privacy import redact_secrets

MAX_TEXT = 50_000
MIN_LEN, MAX_LEN = 8, 240


@dataclass
class Candidate:
    kind: str            # preference | decision | stack | file | note | command | error | error_fix
    subject: str         # user | project
    predicate: str
    object: str
    confidence: float
    rule: str
    single_valued: bool = False


@dataclass
class Stats:
    """Counters for diagnosing why a pipeline yields nothing. Never holds content."""
    sentences: int = 0
    rules_evaluated: int = 0
    matches: int = 0
    rejected_question: int = 0
    rejected_hypothetical: int = 0
    rejected_secret: int = 0
    rejected_quality: int = 0
    deduplicated: int = 0
    accepted: int = 0
    by_rule: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def _h(text: str) -> str:
    return hashlib.sha1(re.sub(r"\W+", " ", text.lower()).strip().encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------
# User-text rules
# ---------------------------------------------------------------------------

LEAD = r"^(?:(?:ok(?:ay)?|also|and|so|btw|please|yes|yeah|now|then|well)[,:]?\s+)*"
_NOT_VERBS = r"(?!know\b|think\b|understand\b|worry\b|see\b|have\b|want\b|need\b|get\b|forget\b|mind\b|care\b|remember\b|like\b)"

# (rule name, kind, compiled regex, confidence)
USER_RULES: list[tuple[str, str, re.Pattern, float]] = [
    ("pref.prefer", "preference",
     re.compile(LEAD + r"(?:i|we)\s+(?:strongly\s+|really\s+|generally\s+)?prefer\s+\S.{2,}", re.I), 0.90),
    ("pref.always_never", "preference",
     re.compile(LEAD + r"(?:(?:i|we|you)\s+)?(?:should\s+)?(?:always|never)\s+(?!mind\b|been\b|knew\b)\w+.{3,}", re.I), 0.85),
    ("pref.dont", "preference",
     re.compile(LEAD + r"(?:do not|don'?t)\s+(?:ever\s+)?" + _NOT_VERBS + r"\w+.{3,}", re.I), 0.75),
    ("pref.from_now_on", "preference",
     re.compile(r"\bfrom now on\b[,:]?\s+\S.{3,}", re.I), 0.90),
    ("pref.my_favourite", "preference",
     re.compile(r"\bmy (?:preferred|favou?rite|default)\s+[\w ]{2,30}\s+is\s+\S.{1,}", re.I), 0.90),
    ("note.remember", "note",
     re.compile(LEAD + r"(?:remember|keep in mind|note)\s+(?:that\s+)?\S.{4,}", re.I), 0.80),

    ("decision.decided", "decision",
     re.compile(r"\b(?:we|i)(?:'ve|\s+have|\s+had)?\s+(?:just\s+)?(?:decided|agreed|chosen|chose)\b.{3,}", re.I), 0.90),
    ("decision.lets", "decision",
     re.compile(LEAD + r"let'?s\s+(?:go with|use|switch to|stick with|standardi[sz]e on|move to|migrate to)\s+\S.{1,}", re.I), 0.80),
    ("decision.we_will", "decision",
     re.compile(LEAD + r"we(?:'ll|\s+will)\s+(?:use|go with|switch to|stick with|migrate to)\s+\S.{1,}", re.I), 0.85),
    ("decision.label", "decision",
     re.compile(r"^\s*(?:decision|decided|chosen approach)\s*:\s*\S.{3,}", re.I), 0.95),
    ("decision.going_forward", "decision",
     re.compile(r"\bgoing forward\b[,:]?\s+\S.{3,}", re.I), 0.80),
    ("decision.instead_of", "decision",
     re.compile(LEAD + r"use\s+[\w.\-+ ]{2,40}\s+instead of\s+[\w.\-+ ]{2,40}", re.I), 0.80),

    ("stack.project_uses", "stack",
     re.compile(r"\b(?:this|the|our)\s+(?:project|repo|codebase|app|service|backend|frontend)\s+"
                r"(?:uses|is built (?:with|on)|runs on|is written in)\s+\S.{1,}", re.I), 0.85),
    ("stack.we_use_for", "stack",
     re.compile(r"\bwe\s+(?:use|run)\s+[\w.\-+]{2,30}\s+for\s+\S.{2,}", re.I), 0.80),

    ("file.path_fact", "file",
     re.compile(r"(?=.*`[\w./\\\-]+\.[A-Za-z0-9]{1,6}`)(?=.*\b(?:contains?|holds?|defines?|handles?|implements?|"
                r"lives? (?:in|at)|is (?:defined|located|configured|stored) (?:in|at)|is responsible for|entry point)\b).{12,}", re.I), 0.80),
]

# Single-valued component rule: "uses X as/for the database" -> predicate uses:<component>
COMPONENT_RULE = re.compile(
    r"\b(?:uses?|using|built (?:with|on))\s+(?P<v>[A-Za-z0-9_.+\- ]{2,40}?)\s+(?:as|for)\s+(?:the |its |our |a )?"
    r"(?P<c>database|db|cache|queue|message broker|orm|web framework|frontend|backend|auth|"
    r"test(?:ing)? framework|package manager|linter|formatter)\b", re.I)
_COMPONENT_NORMAL = {"db": "database", "testing framework": "test framework"}

_QUESTION_END = re.compile(r"\?\s*$")
_HYPOTHETICAL = re.compile(
    r"^\s*(?:if|should|would|could|can|may|might|whether|maybe|perhaps|what|how|why|when|do you|are you|is it|"
    r"does|did|will you|shall)\b", re.I)
_CODEY = re.compile(r"^\s*(?:```|\$ |>>> |#!|import |from \w+ import |def |class )|=>|</?\w+>")


def _sentences(text: str) -> list[str]:
    out = []
    for raw in re.split(r"(?<=[.!?])\s+(?=[A-Z`'\"(])|\n+", text):
        s = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", raw).strip()
        if s:
            out.append(s)
    return out


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip().rstrip(",;:")
    return s[:MAX_LEN].rstrip()


def _validate(obj: str, stats: Stats) -> bool:
    if not (MIN_LEN <= len(obj) <= MAX_LEN):
        stats.rejected_quality += 1
        return False
    if redact_secrets(obj) != obj:
        stats.rejected_secret += 1
        return False
    return True


def extract_text(text: str, stats: Stats | None = None) -> list[Candidate]:
    """Candidates from USER-authored text."""
    stats = stats or Stats()
    if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
        return []
    cands: dict[tuple, Candidate] = {}
    for sentence in _sentences(text):
        stats.sentences += 1
        if _QUESTION_END.search(sentence):
            stats.rejected_question += 1
            continue
        if _HYPOTHETICAL.match(sentence) or _CODEY.search(sentence):
            stats.rejected_hypothetical += 1
            continue
        m = COMPONENT_RULE.search(sentence)
        stats.rules_evaluated += 1
        if m:
            comp = m.group("c").lower()
            comp = _COMPONENT_NORMAL.get(comp, comp)
            val = _clean(m.group("v"))
            stats.matches += 1
            if _validate(val, stats) or (2 <= len(val) < MIN_LEN and redact_secrets(val) == val):
                c = Candidate("stack", "project", f"uses:{comp}", val, 0.90, "stack.component", True)
                cands[(c.subject, c.predicate)] = c
        for name, kind, rx, conf in USER_RULES:
            stats.rules_evaluated += 1
            if not rx.search(sentence):
                continue
            stats.matches += 1
            obj = _clean(sentence)
            if not _validate(obj, stats):
                continue
            subject = "user" if kind in ("preference", "note") else "project"
            c = Candidate(kind, subject, f"{kind}:{_h(obj)}", obj, conf, name)
            key = (c.subject, c.predicate)
            if key in cands:
                stats.deduplicated += 1
            else:
                cands[key] = c
            break  # one rule per sentence: first (most specific) rule wins
    out = list(cands.values())
    stats.accepted += len(out)
    for c in out:
        stats.by_rule[c.rule] = stats.by_rule.get(c.rule, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Tool-result rules
# ---------------------------------------------------------------------------

_CD_PREFIX = re.compile(r"^\s*cd\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*(?:&&|;)\s*")
_TEST_CMD = re.compile(
    r"\b(?:pytest|py\.test|python[\d.]* -m (?:pytest|unittest)|npm (?:run )?test|pnpm (?:run )?test|yarn test|"
    r"jest|vitest|cargo test|go test|dotnet test|mvn test|gradle test|rspec|phpunit)\b")
_PASS_EVIDENCE = [
    re.compile(r"\b(\d+) passed\b"),                       # pytest
    re.compile(r"\bTests?:\s+(?:\d+ \w+, )*(\d+) passed"),  # jest / vitest
    re.compile(r"^ok\s+\S+", re.M),                         # go test
    re.compile(r"test result: ok\. (\d+) passed"),          # cargo
    re.compile(r"\bRan (\d+) tests?\b.*?\n\s*OK\b", re.S),  # unittest
]
_FAIL_EVIDENCE = re.compile(r"\b(\d+) (?:failed|errors?)\b|^FAIL\b|^--- FAIL|Traceback \(most recent call last\)", re.M)
_ERROR_LINE = re.compile(
    r"^(?:E\s+)?(?P<l>(?:[A-Za-z_.]*(?:Error|Exception)\b.*|npm ERR!.*|fatal:.*|error(?: TS\d+)?:.*|"
    r".*command not found.*|.*No such file or directory.*|.*Permission denied.*|ModuleNotFoundError.*))$", re.M)


def normalize_command(cmd: str) -> str:
    if not isinstance(cmd, str):
        return ""
    cmd = _CD_PREFIX.sub("", cmd)
    cmd = re.sub(r"\s+", " ", cmd).strip()
    return cmd[:120]


def _tool_text(tool_response) -> str:
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        parts = [str(tool_response.get(k, "")) for k in ("stdout", "stderr", "output", "error", "content")]
        return "\n".join(p for p in parts if p)
    return str(tool_response or "")


def extract_tool(tool_name: str, tool_input: dict, tool_response, stats: Stats | None = None) -> list[Candidate]:
    """Candidates from a TOOL RESULT (evidence-based: success/failure comes from output)."""
    stats = stats or Stats()
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return []
    cmd = normalize_command(tool_input.get("command", ""))
    if not cmd or not _TEST_CMD.search(cmd):
        return []
    stats.rules_evaluated += 1
    if redact_secrets(cmd) != cmd:
        stats.rejected_secret += 1
        return []
    out = _tool_text(tool_response)
    if len(out) > MAX_TEXT:
        out = out[-MAX_TEXT:]
    failed = bool(_FAIL_EVIDENCE.search(out))
    passed = (not failed) and any(rx.search(out) for rx in _PASS_EVIDENCE)
    if passed:
        stats.matches += 1
        stats.accepted += 1
        stats.by_rule["cmd.test_pass"] = stats.by_rule.get("cmd.test_pass", 0) + 1
        return [Candidate("command", "project", f"command:{_h(cmd)}",
                          f"`{cmd}` runs the tests successfully", 0.95, "cmd.test_pass", True)]
    if failed:
        m = _ERROR_LINE.search(out)
        detail = _clean(m.group("l")) if m else (_clean(_FAIL_EVIDENCE.search(out).group(0)))
        detail = detail[:140]
        if redact_secrets(detail) != detail:
            stats.rejected_secret += 1
            return []
        stats.matches += 1
        stats.accepted += 1
        stats.by_rule["cmd.test_fail"] = stats.by_rule.get("cmd.test_fail", 0) + 1
        return [Candidate("error", "project", f"error:{_h(cmd)}",
                          f"`{cmd}` failed: {detail}", 0.70, "cmd.test_fail", True)]
    return []


# ---------------------------------------------------------------------------
# Optional LLM hook (off by default; same validation applies)
# ---------------------------------------------------------------------------

_llm_extractor: Callable[[str], list[dict]] | None = None


def register_llm_extractor(fn: Callable[[str], list[dict]] | None) -> None:
    """fn(text) -> [{"kind","subject","object","confidence"}]. Text is redacted before the call."""
    global _llm_extractor
    _llm_extractor = fn


def extract_llm(text: str, stats: Stats | None = None, min_confidence: float = 0.75,
                max_candidates: int = 10) -> list[Candidate]:
    stats = stats or Stats()
    if _llm_extractor is None or not text:
        return []
    try:
        raw = _llm_extractor(redact_secrets(text)[:MAX_TEXT]) or []
    except Exception:
        return []
    out = []
    for item in raw[:max_candidates]:
        try:
            kind = str(item["kind"])
            obj = _clean(str(item["object"]))
            conf = float(item.get("confidence", 0))
        except Exception:
            continue
        if kind not in {"preference", "decision", "stack", "file", "note"} or conf < min_confidence:
            continue
        if not _validate(obj, stats):
            continue
        subject = "user" if kind in ("preference", "note") else "project"
        out.append(Candidate(kind, subject, f"{kind}:{_h(obj)}", obj, min(conf, 0.85), "llm"))
    return out
