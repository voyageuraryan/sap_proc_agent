"""Aggregate scored cases into the numbers a human actually asks for.

Three audiences, one object:

  * the terminal, while you iterate
  * a Markdown file, to commit or paste into a PR
  * JSON, for CI to diff against the previous run

Metrics are computed as properties rather than stored, so a report loaded from
JSON and a report just produced cannot disagree.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from agent.cost import format_usd

from evals.safety import SafetyReport
from evals.scoring import CaseResult

#: Column order for the per-label table. Easiest first, so the interesting
#: rows are at the bottom where the eye stops.
LABEL_ORDER = (
    "CLEAN",
    "PRICE_MINOR",
    "PRICE_MAJOR",
    "QTY_OVER",
    "GR_MISSING",
    "GR_PARTIAL",
    "DUP_INVOICE",
    "AMBIGUOUS",
)


@dataclass
class EvalReport:
    split: str
    model: str
    mode: str
    cases: list[CaseResult] = field(default_factory=list)
    safety: SafetyReport = field(default_factory=SafetyReport)
    started_at: str = ""
    finished_at: str = ""
    notes: list[str] = field(default_factory=list)

    # -- headline ----------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.cases)

    def _rate(self, count: int) -> float:
        return count / self.total if self.total else 0.0

    @property
    def submitted_rate(self) -> float:
        """How often the agent produced an answer at all.

        Reported first because every other percentage is conditional on it: a
        90% accuracy over the half of cases that did not crash is not 90%.
        """
        return self._rate(sum(1 for c in self.cases if c.submitted))

    @property
    def classification_accuracy(self) -> float:
        return self._rate(sum(1 for c in self.cases if c.classification_correct))

    @property
    def decision_accuracy(self) -> float:
        """Ideal or acceptable. The headline."""
        return self._rate(sum(1 for c in self.cases if c.decision_ok))

    @property
    def ideal_rate(self) -> float:
        return self._rate(sum(1 for c in self.cases if c.decision_grade == "ideal"))

    @property
    def over_escalation_rate(self) -> float:
        """Punted on something resolvable. The metric that decides adoption."""
        return self._rate(sum(1 for c in self.cases if c.over_escalated))

    @property
    def unsafe_count(self) -> int:
        """Acted where a human was required. Must be zero."""
        return sum(1 for c in self.cases if c.unsafe_action)

    @property
    def correction_precision(self) -> float | None:
        """Of the corrections proposed, how many had the right type AND figure.

        None when nothing was proposed -- distinct from 0.0, which would mean
        everything proposed was wrong.
        """
        proposed = [c for c in self.cases if c.correction_type_correct is not None]
        if not proposed:
            return None
        good = sum(1 for c in proposed if c.correction_type_correct and c.correction_value_correct)
        return good / len(proposed)

    # -- breakdowns --------------------------------------------------------

    def by_label(self) -> dict[str, dict]:
        rows: dict[str, dict] = {}
        for label in LABEL_ORDER:
            subset = [c for c in self.cases if c.label == label]
            if not subset:
                continue
            n = len(subset)
            rows[label] = {
                "n": n,
                "ideal": sum(1 for c in subset if c.decision_grade == "ideal"),
                "acceptable": sum(1 for c in subset if c.decision_grade == "acceptable"),
                "wrong": sum(1 for c in subset if c.decision_grade == "wrong"),
                "classification": sum(1 for c in subset if c.classification_correct) / n,
                "decision": sum(1 for c in subset if c.decision_ok) / n,
                "unsafe": sum(1 for c in subset if c.unsafe_action),
            }
        return rows

    def confusion(self) -> dict[str, Counter]:
        """Ground truth -> what the agent decided. Shows the SHAPE of failure."""
        matrix: dict[str, Counter] = {}
        for case in self.cases:
            matrix.setdefault(case.label, Counter())[case.decision or "NO_ANSWER"] += 1
        return matrix

    def failures(self) -> list[CaseResult]:
        return [c for c in self.cases if not c.decision_ok]

    # -- cost --------------------------------------------------------------

    @property
    def total_usd(self) -> Decimal | None:
        costs = [c.total_usd for c in self.cases]
        if any(c is None for c in costs):
            return None
        return sum(costs, Decimal(0))

    @property
    def usd_per_case(self) -> Decimal | None:
        total = self.total_usd
        return None if total is None or not self.total else total / self.total

    @property
    def usd_per_correct_decision(self) -> Decimal | None:
        """The number a finance reviewer asks for.

        Cost per CORRECT decision, not per call: an agent that is cheap and
        wrong buys nothing, and this is the metric where that shows up.
        """
        total = self.total_usd
        correct = sum(1 for c in self.cases if c.decision_ok)
        return None if total is None or not correct else total / correct

    @property
    def total_tokens(self) -> tuple[int, int]:
        return (
            sum(c.prompt_tokens for c in self.cases),
            sum(c.completion_tokens for c in self.cases),
        )

    @property
    def mean_iterations(self) -> float:
        return sum(c.iterations for c in self.cases) / self.total if self.total else 0.0

    @property
    def mean_duration_ms(self) -> float:
        return sum(c.duration_ms for c in self.cases) / self.total if self.total else 0.0

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "split": self.split,
            "model": self.model,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "totals": {
                "cases": self.total,
                "submitted_rate": round(self.submitted_rate, 4),
                "classification_accuracy": round(self.classification_accuracy, 4),
                "decision_accuracy": round(self.decision_accuracy, 4),
                "ideal_rate": round(self.ideal_rate, 4),
                "over_escalation_rate": round(self.over_escalation_rate, 4),
                "unsafe_actions": self.unsafe_count,
                "correction_precision": (
                    None
                    if self.correction_precision is None
                    else round(self.correction_precision, 4)
                ),
                "prompt_tokens": self.total_tokens[0],
                "completion_tokens": self.total_tokens[1],
                "total_usd": None if self.total_usd is None else str(self.total_usd),
                "usd_per_case": None if self.usd_per_case is None else str(self.usd_per_case),
                "usd_per_correct_decision": (
                    None
                    if self.usd_per_correct_decision is None
                    else str(self.usd_per_correct_decision)
                ),
                "mean_iterations": round(self.mean_iterations, 2),
                "mean_duration_ms": round(self.mean_duration_ms, 1),
            },
            "by_label": self.by_label(),
            "safety": {
                "passed": self.safety.passed,
                "failures": self.safety.failures(),
            },
            "cases": [
                {k: (str(v) if isinstance(v, Decimal) else v) for k, v in case.__dict__.items()}
                for case in self.cases
            ],
            "notes": self.notes,
        }


def _pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


def render_terminal(report: EvalReport) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"split      {report.split}  ({report.total} cases)")
    add(f"model      {report.model}")
    add(f"mode       {report.mode}")
    add("")

    verdict = "PASS" if report.safety.passed else "FAIL"
    add(f"safety     {verdict}")
    for failure in report.safety.failures():
        add(f"           ! {failure}")
    add("")

    add("accuracy")
    add(f"  submitted            {_pct(report.submitted_rate)}")
    add(f"  decision (ok)        {_pct(report.decision_accuracy)}")
    add(f"  decision (ideal)     {_pct(report.ideal_rate)}")
    add(f"  classification       {_pct(report.classification_accuracy)}")
    add(f"  over-escalation      {_pct(report.over_escalation_rate)}")
    add(f"  unsafe actions       {report.unsafe_count}")
    precision = report.correction_precision
    add("  correction precision " + ("     n/a" if precision is None else _pct(precision)))
    add("")

    add(f"  {'label':<12} {'n':>3}  {'ideal':>5} {'ok':>4} {'wrong':>5}  {'decision':>8}  unsafe")
    for label, row in report.by_label().items():
        add(
            f"  {label:<12} {row['n']:>3}  {row['ideal']:>5} {row['acceptable']:>4} "
            f"{row['wrong']:>5}  {_pct(row['decision']):>8}  {row['unsafe']:>6}"
        )
    add("")

    add("cost")
    prompt_tokens, completion_tokens = report.total_tokens
    add(f"  tokens               {prompt_tokens} in / {completion_tokens} out")
    add(f"  total                {format_usd(report.total_usd)}")
    add(f"  per case             {format_usd(report.usd_per_case)}")
    add(f"  per correct decision {format_usd(report.usd_per_correct_decision)}")
    add(f"  mean iterations      {report.mean_iterations:.2f}")
    add(f"  mean wall time       {report.mean_duration_ms:.0f} ms")

    failures = report.failures()
    if failures:
        add("")
        add(f"failures ({len(failures)})")
        for case in failures:
            flag = "UNSAFE" if case.unsafe_action else ("OVER" if case.over_escalated else "")
            add(
                f"  {case.scenario_id}  {case.label:<12} "
                f"expected-ideal, got {case.decision or case.stop_reason:<18} {flag}"
            )
    for note in report.notes:
        add("")
        add(f"note: {note}")
    return "\n".join(lines)


def render_markdown(report: EvalReport) -> str:
    lines: list[str] = []
    add = lines.append

    add(f"# Eval report — `{report.split}`")
    add("")
    add(f"- model: `{report.model}`")
    add(f"- mode: `{report.mode}`")
    add(f"- cases: {report.total}")
    add(f"- run: {report.started_at} → {report.finished_at}")
    add("")

    add(f"## Safety: {'PASS' if report.safety.passed else '**FAIL**'}")
    add("")
    if report.safety.passed:
        add("No invoice changed, no proposal reached APPLIED, no ground truth leaked.")
    else:
        for failure in report.safety.failures():
            add(f"- **{failure}**")
    add("")

    add("## Headline")
    add("")
    add("| metric | value |")
    add("| --- | ---: |")
    add(f"| submitted a resolution | {_pct(report.submitted_rate)} |")
    add(f"| decision ok (ideal or acceptable) | {_pct(report.decision_accuracy)} |")
    add(f"| decision ideal | {_pct(report.ideal_rate)} |")
    add(f"| classification correct | {_pct(report.classification_accuracy)} |")
    add(f"| over-escalation | {_pct(report.over_escalation_rate)} |")
    add(f"| unsafe actions | {report.unsafe_count} |")
    precision = report.correction_precision
    add(f"| correction precision | {'n/a' if precision is None else _pct(precision)} |")
    add(f"| cost per correct decision | {format_usd(report.usd_per_correct_decision)} |")
    add("")

    add("## By label")
    add("")
    add("| label | n | ideal | acceptable | wrong | decision ok | classification | unsafe |")
    add("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for label, row in report.by_label().items():
        add(
            f"| {label} | {row['n']} | {row['ideal']} | {row['acceptable']} | {row['wrong']} "
            f"| {_pct(row['decision'])} | {_pct(row['classification'])} | {row['unsafe']} |"
        )
    add("")

    add("## Where it went wrong")
    add("")
    failures = report.failures()
    if not failures:
        add("Nothing. Every case landed on an ideal or acceptable decision.")
    else:
        add("| scenario | label | decision | flag | reasoning |")
        add("| --- | --- | --- | --- | --- |")
        for case in failures:
            flag = "UNSAFE" if case.unsafe_action else ""
            if not flag and case.over_escalated:
                flag = "over-escalated"
            reason = (case.reasoning or case.stop_reason).replace("|", "\\|")[:120]
            add(
                f"| {case.scenario_id} | {case.label} | "
                f"{case.decision or case.stop_reason} | {flag} | {reason} |"
            )
    add("")

    add("## Cost")
    add("")
    prompt_tokens, completion_tokens = report.total_tokens
    add(f"- tokens: {prompt_tokens} in / {completion_tokens} out")
    add(f"- total: {format_usd(report.total_usd)}")
    add(f"- per case: {format_usd(report.usd_per_case)}")
    add(f"- per correct decision: {format_usd(report.usd_per_correct_decision)}")
    add(f"- mean iterations: {report.mean_iterations:.2f}")
    for note in report.notes:
        add("")
        add(f"> {note}")
    return "\n".join(lines) + "\n"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
