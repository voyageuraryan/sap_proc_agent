"""The demo, as a program.

A recorded walkthrough that fumbles is worse than no video, so the demo is a
script rather than a set of instructions. It runs the same seven acts every
time, in the same order, at a readable pace, and every number it prints is
read live from the running services -- nothing here is staged.

    uv run uvicorn mock_erp.app:app --port 8000
    uv run uvicorn review_ui.app:app --port 8001
    uv run python scripts/demo.py                 # rule baseline, no API key
    uv run python scripts/demo.py --mode live     # a real model
    uv run python scripts/demo.py --no-pause      # for asciinema / CI

The story it tells, in order:
    1. the queue        -- what an AP clerk actually faces
    2. the trap         -- two invoices that look identical and are not
    3. the agent        -- what it reads and what it concludes
    4. the wall         -- the agent cannot write, demonstrated not asserted
    5. the human        -- approval, and the moment the document changes
    6. the evidence     -- the trace and what the run cost
    7. the score        -- 200 scenarios, and the safety gates
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal

import httpx

ODATA = "/sap/opu/odata/sap/ZPROC_SRV"

#: The matched pair the whole project exists to separate. Same purchase order
#: shape, same short receipt; only the billed quantity differs.
OVER_INVOICED = "5100000901"  # SC-0009  QTY_OVER    ordered 14, received 13, billed 14
PARTIAL = "5100001801"  # SC-0018  GR_PARTIAL ordered 13, received 1,  billed 1

RULE = "\033[2m" + "─" * 74 + "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
OFF = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
AMBER = "\033[33m"


class Demo:
    def __init__(self, erp: str, ui: str, pause: float):
        self.erp = erp.rstrip("/")
        self.ui = ui.rstrip("/")
        self.pause = pause
        self.http = httpx.Client(timeout=30.0, follow_redirects=False)

    # -- presentation ----------------------------------------------------

    def act(self, number: int, title: str, subtitle: str = "") -> None:
        print(f"\n{RULE}")
        print(f"{BOLD}  {number}. {title}{OFF}")
        if subtitle:
            print(f"{DIM}     {subtitle}{OFF}")
        print(f"{RULE}\n")
        self.beat()

    def say(self, text: str = "") -> None:
        print(f"  {text}")

    def beat(self, factor: float = 1.0) -> None:
        if self.pause:
            time.sleep(self.pause * factor)

    # -- reading the ERP -------------------------------------------------

    def invoice(self, number: str) -> dict:
        return self.http.get(f"{self.erp}{ODATA}/A_SupplierInvoice('{number}')").json()["d"]

    def po(self, number: str) -> dict:
        return self.http.get(f"{self.erp}{ODATA}/A_PurchaseOrder('{number}')").json()["d"]

    def receipts(self, po_number: str) -> list[dict]:
        response = self.http.get(
            f"{self.erp}{ODATA}/A_MaterialDocumentItem",
            params={"$filter": f"PurchaseOrder eq '{po_number}'"},
        )
        return response.json()["d"]["results"]

    def line(self, invoice_number: str) -> dict:
        return self.invoice(invoice_number)["items"][0]

    def quantity(self, invoice_number: str) -> str:
        return self.line(invoice_number)["MENGE"]

    # -- the acts --------------------------------------------------------

    def act_1_the_queue(self) -> None:
        self.act(
            1,
            "The queue",
            "212 supplier invoices. Someone opens each one and decides.",
        )
        health = self.http.get(f"{self.erp}/healthz").json()
        for key in ("purchase_orders", "goods_receipts", "invoices"):
            self.say(f"{key.replace('_', ' '):<18} {health[key]:>5}")
        blocked = self.http.get(f"{self.erp}{ODATA}/A_SupplierInvoice").json()["d"]["results"]
        held = [inv for inv in blocked if inv.get("block_reason")]
        self.say()
        self.say(f"{len(held)} of them are blocked and waiting on a human.")
        self.beat(2)

    def act_2_the_trap(self) -> None:
        self.act(
            2,
            "The trap",
            "Two invoices. One is fraud-adjacent, one is perfectly normal.",
        )
        for label, number in (("A", OVER_INVOICED), ("B", PARTIAL)):
            invoice = self.invoice(number)
            item = invoice["items"][0]
            po = self.po(item["EBELN"])
            received = sum(
                (Decimal(gr["MENGE"]) for gr in self.receipts(item["EBELN"])), Decimal(0)
            )
            po_line = po["items"][0]
            self.say(f"{BOLD}Invoice {label}: {number}{OFF}")
            self.say(f"  ordered   {po_line['MENGE']:>10}   (PO {po['EBELN']})")
            self.say(f"  received  {received!s:>10}")
            self.say(f"  billed    {item['MENGE']:>10}")
            self.say(f"  blocked   {invoice.get('block_reason') or '—':>10}")
            self.say()
        self.say("Both were short-delivered. Only one is over-billed.")
        self.say(f"{DIM}Telling them apart is the whole job. The eval set has 36 of these.{OFF}")
        self.beat(3)

    def act_3_the_agent(self, mode: str) -> tuple[str, dict]:
        self.act(3, "The agent", f"Five tools, an iteration cap, and a typed verdict. ({mode})")
        run = run_agent_once(self.erp, OVER_INVOICED, mode)

        for index, call in enumerate(run["tool_calls"], 1):
            mark = f"{RED}x{OFF}" if call["error"] else f"{GREEN}·{OFF}"
            args = ", ".join(f"{k}={v}" for k, v in call["arguments"].items())[:56]
            self.say(f"{mark} {index}. {call['name']}({args})")
        self.say()

        resolution = run["resolution"]
        self.say(f"{BOLD}{resolution['classification']}{OFF} → {BOLD}{resolution['decision']}{OFF}")
        self.say(f"{resolution['reasoning']}")
        self.say()
        for item in resolution["evidence"]:
            self.say(f"{DIM}  · {item}{OFF}")
        self.beat(3)
        return run["invoice_number"], run

    def act_4_the_wall(self) -> str:
        self.act(
            4,
            "The wall",
            "The agent raised a proposal. It cannot act on it.",
        )
        before = self.quantity(OVER_INVOICED)
        self.say(f"invoice quantity right now          {BOLD}{before}{OFF}")
        self.say()

        proposals = self.http.get(f"{self.erp}/approval/proposals").json()["d"]["results"]
        proposal = proposals[-1]
        self.say(f"proposal {proposal['proposal_id']} status {proposal['status']}")
        self.say(f"payload hash {DIM}{proposal['payload_hash'][:32]}…{OFF}")
        self.say()

        self.say(f"{DIM}Trying to apply it without an approval:{OFF}")
        response = self.http.post(
            f"{self.erp}{ODATA}/ApplyCorrection",
            json={"proposal_id": proposal["proposal_id"], "payload": proposal["payload"]},
        )
        code = response.json().get("error", {}).get("code", "?")
        self.say(f"  {RED}{response.status_code} {code}{OFF}")
        self.say(f"  quantity still {self.quantity(OVER_INVOICED)}")
        self.say()

        self.say("The agent's tool list contains no way to approve or apply.")
        self.say(f"{DIM}Not refused at runtime — absent from the schema the model reads.{OFF}")
        self.beat(3)
        return proposal["proposal_id"]

    def act_5_the_human(self, proposal_id: str) -> None:
        self.act(5, "The human", f"{self.ui}/proposals/{proposal_id}")
        page = self.http.get(f"{self.ui}/proposals/{proposal_id}").text
        payload_hash = _form_hash(page)
        self.say("The reviewer sees the change, the reasoning, and the evidence")
        self.say("unsummarised: the PO line, every receipt, and this supplier's")
        self.say("tolerance. Enough to disagree.")
        self.say()

        self.say(f"{DIM}Approving from a page that is out of date:{OFF}")
        stale = self.http.post(
            f"{self.ui}/proposals/{proposal_id}/approve",
            data={"reviewer": "ap.supervisor@example.com", "payload_hash": "0" * 64},
        )
        self.say(f"  {AMBER}{_error_from(stale)}{OFF}  — you approve what you were shown")
        self.say()

        self.http.post(
            f"{self.ui}/proposals/{proposal_id}/approve",
            data={"reviewer": "ap.supervisor@example.com", "payload_hash": payload_hash},
        )
        self.say(f"{GREEN}approved{OFF} by ap.supervisor@example.com")
        self.say(
            f"  quantity {BOLD}{self.quantity(OVER_INVOICED)}{OFF}  "
            f"{DIM}— approval is not application{OFF}"
        )
        self.beat(1.5)

        self.say()
        self.say(f"{DIM}Now applying a payload the human never approved:{OFF}")
        proposal = self.http.get(f"{self.erp}/approval/proposals/{proposal_id}").json()["d"]
        tampered = dict(proposal["payload"])
        for key in ("to_quantity", "to_price"):
            if key in tampered:
                tampered[key] = "1.000"
        response = self.http.post(
            f"{self.erp}{ODATA}/ApplyCorrection",
            json={"proposal_id": proposal_id, "payload": tampered},
        )
        self.say(
            f"  {RED}{response.status_code} "
            f"{response.json().get('error', {}).get('code', '?')}{OFF}"
            f"  {DIM}— approving a payload is not approving any payload{OFF}"
        )
        self.say(f"  quantity still {self.quantity(OVER_INVOICED)}")
        self.beat(1.5)

        self.http.post(
            f"{self.ui}/proposals/{proposal_id}/apply", data={"payload_hash": payload_hash}
        )
        self.say(f"{GREEN}applied{OFF}")
        self.say(
            f"  quantity {BOLD}{self.quantity(OVER_INVOICED)}{OFF}  "
            f"{DIM}— the first and only write{OFF}"
        )
        self.say()
        self.say("Visible on the same URL the agent read from.")
        self.beat(3)

    def act_6_the_evidence(self, run: dict) -> None:
        self.act(6, "The evidence", "Every run is traced and priced.")
        free = run["prompt_tokens"] == 0

        if free:
            self.say(f"This run used the {BOLD}rule baseline{OFF}: no model, no tokens, no cost.")
            self.say(f"{DIM}It is the floor the model has to beat — 6 tool calls, ~25 ms, $0.{OFF}")
            self.say(f"{DIM}Re-run with --mode live to see the real curve.{OFF}")
            self.say()

        self.say(f"{'#':>2}  {'in':>7} {'out':>6}  {'usd':>11}")
        for call in run["llm_calls"]:
            usd = "free"
            if call.get("input_usd") is not None and call.get("output_usd") is not None:
                usd = f"${Decimal(call['input_usd']) + Decimal(call['output_usd']):.6f}"
            self.say(
                f"{call['iteration']:>2}  {call['prompt_tokens']:>7} "
                f"{call['completion_tokens']:>6}  {usd:>11}"
            )
        total = run.get("total_usd")
        self.say(
            f"{'':>2}  {run['prompt_tokens']:>7} {run['completion_tokens']:>6}  "
            f"{('$' + f'{Decimal(total):.6f}') if total is not None else 'unpriced':>11}"
        )
        self.say()

        if not free:
            self.say(f"{DIM}Input tokens grow every turn — the whole transcript is re-sent,{OFF}")
            self.say(f"{DIM}so cost is roughly quadratic in tool calls. That is the number{OFF}")
            self.say(f"{DIM}to quote at 10,000 invoices a month.{OFF}")
        else:
            self.say(f"{DIM}With a real model the input column grows every turn — the whole{OFF}")
            self.say(
                f"{DIM}transcript is re-sent — so cost is roughly quadratic in tool calls.{OFF}"
            )
        self.say()
        self.say(
            f"{DIM}Spans also go to a local JSONL file or to Langfuse: "
            f"--trace-file traces/run.jsonl{OFF}"
        )
        self.beat(3)

    def act_7_the_score(self) -> None:
        self.act(7, "The score", "200 labelled scenarios. Safety is a build gate.")
        self.say("$ uv run proc-evals --split all --mode baseline")
        self.say()
        self.say(f"{DIM}(run it alongside — the harness scores every scenario against{OFF}")
        self.say(f"{DIM} ground truth the agent structurally cannot reach, and exits{OFF}")
        self.say(f"{DIM} non-zero if any invoice changed or any label leaked){OFF}")
        self.beat(2)

    def close(self) -> None:
        print(f"\n{RULE}")
        print(f"{BOLD}  95% of enterprise AI pilots fail on integration, not on models.{OFF}")
        print(f"{BOLD}  So this is the integration-hard version.{OFF}")
        print(f"{RULE}\n")


def _form_hash(html: str) -> str:
    import re

    match = re.search(r'name="payload_hash" value="([0-9a-f]+)"', html)
    if not match:
        raise SystemExit("the review page rendered no payload hash — is the proposal PROPOSED?")
    return match.group(1)


def _error_from(response: httpx.Response) -> str:
    location = response.headers.get("location", "")
    if "error_code=" in location:
        return location.split("error_code=")[1].split("&")[0]
    return str(response.status_code)


def run_agent_once(erp: str, invoice_number: str, mode: str) -> dict:
    """Run the agent and hand back its AgentRun as a dict.

    Imported lazily so the demo can be read without the workspace installed.
    """
    from agent.erp_client import ErpClient
    from agent.loop import run_agent
    from agent.settings import AgentSettings

    settings = AgentSettings(erp_base_url=f"{erp}{ODATA}", tracing=False)
    completion_fn = None
    if mode == "baseline":
        from evals.baseline import baseline_completion

        completion_fn = baseline_completion
        settings = settings.model_copy(update={"model": "baseline/rules"})

    with ErpClient(settings.erp_base_url, settings.request_timeout) as client:
        run = run_agent(
            invoice_number, client, settings, scenario_id="SC-0009", completion_fn=completion_fn
        )
    if run.resolution is None:
        raise SystemExit(f"the agent did not submit a resolution ({run.stop_reason.value})")
    return json.loads(run.model_dump_json())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the demo end to end.")
    parser.add_argument("--erp", default="http://127.0.0.1:8000")
    parser.add_argument("--ui", default="http://127.0.0.1:8001")
    parser.add_argument(
        "--mode",
        default="baseline",
        choices=("baseline", "live"),
        help="baseline needs no API key and is identical every run",
    )
    parser.add_argument("--pause", type=float, default=0.9, help="Seconds between beats")
    parser.add_argument("--no-pause", action="store_true")
    args = parser.parse_args(argv)

    demo = Demo(args.erp, args.ui, 0.0 if args.no_pause else args.pause)
    try:
        demo.http.get(f"{args.erp}/healthz").raise_for_status()
        demo.http.get(f"{args.ui}/healthz").raise_for_status()
    except (httpx.HTTPError, httpx.RequestError) as exc:
        print(f"both services must be running first ({exc})", file=sys.stderr)
        print("  uv run uvicorn mock_erp.app:app --port 8000", file=sys.stderr)
        print("  uv run uvicorn review_ui.app:app --port 8001", file=sys.stderr)
        return 2

    demo.act_1_the_queue()
    demo.act_2_the_trap()
    _, run = demo.act_3_the_agent(args.mode)
    proposal_id = demo.act_4_the_wall()
    demo.act_5_the_human(proposal_id)
    demo.act_6_the_evidence(run)
    demo.act_7_the_score()
    demo.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
