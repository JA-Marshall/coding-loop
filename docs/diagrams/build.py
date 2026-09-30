#!/usr/bin/env python3
"""Draws the README diagrams, each in a light and a dark variant.

    python3 docs/diagrams/build.py

Standard library only. The SVG files next to this script are its output: edit
the figure functions below and rerun, do not edit the SVG by hand.
"""
from html import escape
from pathlib import Path

SANS = "'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
MONO = "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

# One entry per colour role: (fill, stroke, text). Amber is a model call, blue is
# GitHub, red is a stop, violet is the router, which learns from outcomes.
THEMES = {
    "light": {
        "bg": "#F3F0E8", "card": "#FCFBF7", "line": "#B9B3A4", "ink": "#1F2126", "muted": "#5C5F67",
        "a": ("#F9E8CB", "#B86E14", "#7E4706"),
        "g": ("#E4ECF6", "#3D6FA8", "#24507F"),
        "s": ("#F8E1DD", "#B5433A", "#96291F"),
        "r": ("#ECE6F7", "#6D4FB0", "#4E3590"),
    },
    "dark": {
        "bg": "#15171C", "card": "#1E2128", "line": "#474B57", "ink": "#E9E7E1", "muted": "#A4A7B0",
        "a": ("#33260F", "#D9983F", "#F0BC74"),
        "g": ("#16273B", "#6CA0DC", "#9CC3EE"),
        "s": ("#3A1B19", "#DD7168", "#F0A29B"),
        "r": ("#2A2142", "#A48AE3", "#C6B4F2"),
    },
}
ROLES = "agsr"

LEGEND = {
    "a": "model call: the only place tokens are spent",
    "p": "plain Python",
    "g": "GitHub API",
    "s": "stop",
    "r": "router: learned from outcomes, no model call of its own",
}


class Figure:
    def __init__(self, width, height, theme, label):
        self.w, self.h, self.t, self.label = width, height, THEMES[theme], label
        self.shapes, self.texts = [], []

    # Shapes are drawn before text so a label is never under a line.
    def box(self, x, y, w, h, kind="p", rx=6, dashed=False, width=None):
        fill, stroke = self._paint(kind)
        width = width or (1.25 if kind in ("p", "sub", "ghost") else 1.75)
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.shapes.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
                           f'stroke="{stroke}" stroke-width="{width}"{dash}/>')

    def _paint(self, kind):
        if kind in ROLES:
            return self.t[kind][0], self.t[kind][1]
        if kind == "sub":
            return self.t["bg"], self.t["line"]
        if kind == "ghost":
            return "none", self.t["line"]
        return self.t["card"], self.t["line"]

    def arrow(self, d, kind="ink", dashed=False, head=True):
        stroke = self.t[kind][1] if kind in ROLES else self.t["ink"]
        dash = ' stroke-dasharray="3 4"' if dashed else ""
        end = f' marker-end="url(#head-{kind})"' if head else ""
        self.shapes.append(f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="1.5" '
                           f'stroke-linejoin="round"{dash}{end}/>')

    def text(self, x, y, string, cls=""):
        cls = f' class="{cls}"' if cls else ""
        self.texts.append(f'<text x="{x}" y="{y}"{cls}>{escape(string, quote=False)}</text>')

    def lines(self, x, y, strings, cls="mu", step=15):
        for index, string in enumerate(strings):
            self.text(x, y + index * step, string, cls)

    def badge(self, cx, cy, number, kind="r"):
        self.shapes.append(f'<circle cx="{cx}" cy="{cy}" r="9" fill="{self.t[kind][1]}"/>')
        self.texts.append(f'<text x="{cx}" y="{cy + 3.8}" class="c b s10" fill="{self.t["bg"]}">{number}</text>')

    def pill(self, right, y, string, kind="r"):
        """A small status tag whose right edge is at `right`."""
        width = round(len(string) * 6.6 + 14)
        fill, stroke = (self.t["card"], self.t["ink"]) if kind == "p" else (self.t["bg"], self.t[kind][1])
        self.shapes.append(f'<rect x="{right - width}" y="{y}" width="{width}" height="17" rx="8.5" '
                           f'fill="{fill}" stroke="{stroke}" stroke-width="1"/>')
        self.text(right - width / 2, y + 12, string, "c b s9" + ("" if kind == "p" else f" c{kind}"))

    def title(self, string, source):
        self.text(24, 34, string, "b s15")
        self.text(self.w - 24, 34, source, "e mu mono s11")

    def legend(self, kinds, y=None):
        x, y = 24, y or self.h - 22
        for kind in kinds:
            self.box(x, y - 10, 12, 12, kind, rx=3, width=1.25)
            self.text(x + 18, y, LEGEND[kind], "mu s11")
            x += 18 + len(LEGEND[kind]) * 5.6 + 20

    def svg(self):
        t = self.t
        heads = "".join(
            f'<marker id="head-{kind}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto"><path d="M0 0.5L10 5L0 9.5z" fill="{t[kind][1] if kind in ROLES else t["ink"]}"/></marker>'
            for kind in ("ink", "s", "r"))
        colours = "".join(f".c{kind}{{fill:{t[kind][2]}}}" for kind in ROLES)
        style = (f"text{{font-family:{SANS};font-size:11.5px;fill:{t['ink']}}}"
                 f".mono{{font-family:{MONO}}}.b{{font-weight:600}}.mu{{fill:{t['muted']}}}"
                 ".c{text-anchor:middle}.e{text-anchor:end}"
                 ".s9{font-size:9.5px;letter-spacing:.05em}.s10{font-size:10.5px}.s11{font-size:11px}"
                 ".s12{font-size:12.5px}.s13{font-size:13px}.s15{font-size:15px}" + colours)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" role="img" '
                f'aria-label="{escape(self.label)}">\n<style>{style}</style>\n<defs>{heads}</defs>\n'
                f'<rect width="{self.w}" height="{self.h}" rx="12" fill="{t["bg"]}"/>\n'
                + "\n".join(self.shapes) + "\n" + "\n".join(self.texts) + "\n</svg>\n")


def phase(fig, x, y, w, h, name, caption, kind="p"):
    """A named phase with its caption inside the box."""
    fig.box(x, y, w, h, kind)
    fig.text(x + 14, y + 24, name, "mono b s13" + (f" c{kind}" if kind in ROLES else ""))
    fig.lines(x + 14, y + 43, caption)


def batch_loop(theme):
    fig = Figure(960, 552, theme,
                 "The batch supervisor takes each packet from a frozen manifest through PREPARE, ROUTE, RUN and "
                 "COMMIT on the local machine, then PUSH, PR, CI and MERGE through the GitHub API, and loops back "
                 "to PREPARE for the next packet from a refreshed origin/main. RUN runs the packet's checks before "
                 "any review, and CI is a second gate on the pushed head. ROUTE is planned: it predicts the "
                 "odds of a one-pass merge and picks the model pair, or a split, before RUN. State is checkpointed "
                 "after every phase, and any failed gate or spent budget stops the queue with its evidence kept.")
    fig.title("Outer loop: one pass per packet", "batch.py")

    fig.box(24, 54, 204, 46)
    fig.text(38, 73, "frozen manifest", "b s12")
    fig.text(38, 89, "4 packets, hashed at the start", "mu")
    fig.arrow("M126 100V129")
    fig.text(136, 119, "packet i", "mu s11")

    top, low, width, height = 130, 290, 204, 70
    columns = (24, 260, 496, 732)
    phase(fig, columns[0], top, width, height, "PREPARE", ["plan file DRAFT → READY →", "IN_PROGRESS, validated"])
    phase(fig, columns[1], top, width, height, "ROUTE", ["predict one-pass odds, pick", "a pair or split · figure 4"], "r")
    fig.pill(columns[1] + width - 10, top + 11, "PLANNED")
    phase(fig, columns[2], top, width, height, "RUN", ["runner.py: patch, checks,", "then review · figure 2"], "a")
    phase(fig, columns[3], top, width, height, "COMMIT", ["plan marked COMPLETE,", "ACTIVE cleared"])
    for x in columns[:3]:
        fig.arrow(f"M{x + width} {top + 35}H{x + width + 31}")

    # The GitHub half runs right to left, so MERGE lands under PREPARE and the loop closes.
    fig.box(12, 262, 936, 110, "g", rx=10, dashed=True, width=1.25)
    fig.text(260, 281, "GitHub API, no model tokens", "b cg")
    fig.arrow(f"M834 {top + height}V{low - 1}")
    phase(fig, columns[3], low, width, height, "PUSH", ["exact ref, never forced"])
    phase(fig, columns[2], low, width, height, "PR", ["create or reuse the PR"])
    phase(fig, columns[1], low, width, height, "CI", ["a second gate: the required", "check on the pushed head"])
    phase(fig, columns[0], low, width, height, "MERGE", ["API merge of the exact SHA"])
    for x in columns[1:]:
        fig.arrow(f"M{x} {low + 35}H{x - 31}")

    fig.arrow(f"M126 {low}V{top + height + 1}")
    fig.text(140, 227, "next packet: index + 1, on a checkout refreshed", "s12")
    fig.text(140, 243, "from origin/main after the verified merge", "s12")

    fig.arrow(f"M126 {low + height}V403")
    fig.box(24, 404, 204, 58)
    fig.text(38, 428, "COMPLETE", "mono b s13")
    fig.text(38, 447, "last packet merged", "mu")

    fig.arrow("M421 372V403", dashed=True, head=False)
    fig.text(430, 392, "every transition", "mu s10")
    fig.box(260, 404, 322, 88)
    fig.text(274, 426, "state.json checkpoint after every phase", "b s12")
    fig.lines(274, 446, ["a restart reconciles local writes with the",
                         "remote PR state: never a second writer,",
                         "never a replay of an ambiguous call"])

    fig.arrow("M700 372V403", "s", dashed=True)
    fig.text(709, 392, "any failed gate or spent budget", "cs s10")
    fig.box(614, 404, 322, 100, "s")
    fig.text(628, 426, "STOPPED, evidence preserved", "b s12 cs")
    fig.lines(628, 446, ["STOP file · 8 h deadline · 24 calls",
                         "4M tokens · unknown usage · branch moved",
                         "failed or stale CI · changes requested",
                         "supervisor files changed"], "cs")
    fig.legend("apgsr")
    return fig


def packet_loop(theme):
    fig = Figure(960, 528, theme,
                 "Inside one packet the runner calls a worker model for a patch, applies it to the owned files "
                 "only, runs the prescribed checks, then calls a reviewer model on the full diff. A bad patch, a "
                 "failed check or a blocking finding sends bounded evidence back to a fresh worker call, at most "
                 "two corrections and six calls. High-risk packets get an independent advisory review whose "
                 "blocking finding stops the run for the owner. Otherwise the candidate is handed back as "
                 "LOCAL_REVIEWED. Every attempt becomes a run record for the router.")
    fig.title("Inner loop: one packet", "runner.py")

    y, width, height = 104, 132, 50
    steps = (("IMPLEMENT", "a", ["worker model,", "fresh process"]),
             ("APPLYING", "p", ["patch applied to", "the owned files only"]),
             ("CHECKS", "p", ["prescribed commands:", "tests, validator"]),
             ("REVIEW", "a", ["Claude Opus 5.5, full", "diff and prior findings"]),
             ("ADVISORY", "a", ["GPT Sol, high-risk only,", "sees no prior findings"]),
             ("LOCAL_REVIEWED", "p", ["handed back to", "the batch loop"]))
    for index, (name, kind, caption) in enumerate(steps):
        x = 24 + index * 156
        fig.box(x, y, width, height, kind, dashed=name == "ADVISORY")
        fig.text(x + width / 2, y + 30, name, "mono b s12 c" + (" ca" if kind == "a" else ""))
        fig.lines(x + width / 2, 76, caption, "mu s11 c")
        if index:
            fig.arrow(f"M{x - 24} {y + 25}H{x - 1}")

    # Each way back is a chip of bounded evidence on one return line.
    evidence = (("bad patch", ["back with the", "parser error"]),
                ("failed check", ["back with ≤8,000 chars", "of its failure blocks"]),
                ("blocking finding", ["back with the", "findings themselves"]))
    for index, (name, caption) in enumerate(evidence):
        centre = 246 + index * 156
        fig.arrow(f"M{centre} {y + height}V185")
        fig.box(centre - 72, 186, 144, 66, "sub")
        fig.text(centre, 206, name, "b c")
        fig.lines(centre, 223, caption, "mu s11 c", step=14)
        fig.arrow(f"M{centre} 252V272", head=False)
    fig.arrow(f"M558 272H90V{y + height + 1}")
    fig.text(104, 293, "back to a fresh IMPLEMENT call with only that evidence: one correction, at most 2", "s12")
    fig.text(104, 309, "each return is a new process, not a resumed session", "mu")

    fig.arrow(f"M50 {y + height}V331", "s")
    fig.box(24, 332, 440, 72, "s")
    fig.text(38, 354, "STOPPED", "b s12 cs")
    fig.lines(38, 374, ["more than 2 corrections · more than 6 calls (7 with advisory)",
                        "empty patch · out-of-scope file"], "cs")

    fig.arrow(f"M714 {y + height}V331", "s")
    fig.lines(724, 262, ["a blocking advisory finding", "never starts a correction"], "mu s11", step=14)
    fig.box(496, 332, 440, 72, "s")
    fig.text(510, 354, "STOPPED for owner review", "b s12 cs")
    fig.lines(510, 374, ["the advisory review raised a blocking finding; the reviewed",
                        "candidate is kept and no PR is opened"], "cs")

    fig.lines(870, 176, ["non-blocking findings", "travel in the PR body", "as reviewer notes"], "mu s11 c", step=14)

    fig.box(24, 426, 912, 56, "r")
    fig.text(38, 448, "Every attempt, however it ends, becomes a run record for the router · figure 4", "b s12 cr")
    fig.text(38, 467, "label (one_pass, corrected, blocked, failed_checks, no_patch) · correction rounds · "
                      "tokens per role · seconds", "mu")
    fig.legend("apsr")
    return fig


def call_context(theme):
    fig = Figure(960, 404, theme,
                 "The supervisor assembles a prompt from a role template, the packet, a manifest of owned file "
                 "names and sizes, and bounded evidence, then starts a fresh restricted process with read-only "
                 "tools over a source snapshot with agent config stripped out. The process returns a JSON result "
                 "and nothing carries over to the next call.")
    fig.title("One model call: what it can see", "isolated.py")

    fig.box(24, 56, 270, 232)
    fig.text(38, 78, "the supervisor builds the prompt", "b s12")
    parts = (("TEMPLATE", "worker.md or reviewer.md"),
             ("PACKET", "objective, owned files, budgets"),
             ("MANIFEST", "file names and byte sizes only"),
             ("EVIDENCE", "≤8,000 chars, or the diff"))
    for index, (name, caption) in enumerate(parts):
        y = 90 + index * 48
        fig.box(38, y, 242, 42, "sub", rx=4)
        fig.text(48, y + 17, name, "mono b s11")
        fig.text(48, y + 33, caption, "mu")

    fig.arrow("M294 172H327")
    fig.text(310, 164, "stdin", "mu s10 c")
    fig.box(328, 56, 336, 232, "a")
    fig.text(342, 78, "one fresh process, empty context", "b s12 ca")
    fig.lines(342, 108, ["claude -p --restricted --tools Read,Grep,Glob",
                         "codex exec --sandbox read-only",
                         "muse exec --no-session-log"], "mono s11", step=20)
    fig.lines(342, 192, ["reads only the symbols it needs",
                         "no write, shell, network or delegation",
                         "no --resume, no prior turns, no compaction",
                         "its output must match a JSON schema"], step=20)

    fig.arrow("M664 172H697")
    fig.text(681, 164, "reads", "mu s10 c")
    fig.box(698, 56, 238, 232)
    fig.text(712, 78, "read-only source snapshot", "b s12")
    fig.lines(712, 100, ["tracked files are copied, with", "these stripped so nothing in the", "repo can override the packet:"])
    for index, row in enumerate(((".claude/", "CLAUDE.md"), (".codex/", ".agents/"), (".git/", ".github/"), (".env*",))):
        for column, name in enumerate(row):
            fig.text(712 + column * 82, 158 + index * 18, name, "mono s11")
    fig.lines(712, 240, ["check logs live outside it, so", "only the EVIDENCE excerpt", "of them is visible"])

    fig.arrow("M496 288V330H159V289")
    fig.text(510, 310, "{patch, summary}  or  {findings[]}", "mono s11")
    fig.text(510, 326, "parsed, size-capped, usage recorded", "mu")
    fig.text(24, 358, "The next call starts from zero: only what the supervisor puts back into the prompt survives.", "s12")
    fig.legend("ap")
    return fig


def routing_loop(theme):
    fig = Figure(960, 640, theme,
                 "The planned routing loop. A predictor estimates the probability that a model pair finishes a "
                 "packet in one pass. A contextual bandit then picks one of three actions: the cheap pair as "
                 "posed, the expensive pair as posed, or the cheap pair after a smarter model splits the packet "
                 "into sub-packets with disjoint file ownership. The inner loop runs the choice, and every attempt "
                 "is written as a run record. The records train the predictor, update the bandit with reward "
                 "success minus lambda times cost, and score each split so the decomposition policy improves. "
                 "Run records exist today; the predictor, bandit and decomposer are planned.")
    fig.title("Routing loop: learn what a cheap pair can finish in one pass", "coding-loop-router")

    fig.box(24, 58, 190, 86)
    fig.text(38, 82, "PACKET", "mono b s13")
    fig.lines(38, 101, ["from the owner's queue,", "or derived from a merged", "PR, judged by its tests"])
    fig.arrow("M214 101H249")

    fig.box(250, 58, 310, 86, "r")
    fig.badge(271, 78, 1)
    fig.text(288, 83, "PREDICT", "mono b s13 cr")
    fig.pill(550, 68, "PLANNED")
    fig.text(264, 106, "p(one_pass | packet features, pair)", "mono s11")
    fig.lines(264, 125, ["calibrated; the features need no model call"])
    fig.arrow("M560 92H595")

    fig.box(596, 58, 340, 86, "r")
    fig.badge(617, 78, 2)
    fig.text(634, 83, "ROUTE", "mono b s13 cr")
    fig.pill(926, 68, "PLANNED")
    fig.lines(610, 106, ["a contextual bandit over three actions:",
                         "Thompson sampling, a few % explored at random"], step=17)

    fig.arrow("M766 144V175")
    fig.text(776, 165, "picks one", "mu s10")
    fig.box(596, 176, 340, 262, "ghost", rx=8, dashed=True)
    fig.box(608, 188, 316, 52)
    fig.text(622, 209, "cheap_direct", "mono b s12")
    fig.text(622, 228, "the cheap pair attempts the packet as posed", "mu")
    fig.box(608, 250, 316, 52)
    fig.text(622, 271, "expensive_direct", "mono b s12")
    fig.text(622, 290, "the expensive pair attempts it as posed", "mu")
    fig.box(608, 312, 316, 114)
    fig.text(622, 333, "cheap_split", "mono b s12")
    fig.text(622, 352, "split first, then the cheap pair on each part", "mu")
    fig.box(622, 364, 134, 50, "a")
    fig.badge(640, 382, 3, "a")
    fig.text(655, 386, "DECOMPOSE", "mono b s11 ca")
    fig.text(634, 404, "a smarter model", "mu s11")
    fig.arrow("M756 389H775")
    for x in (780, 802, 824):
        fig.box(x, 380, 18, 18, "sub", rx=3)
    fig.lines(852, 386, ["disjoint file", "ownership"], "mu s11", step=14)

    fig.arrow("M766 438V469")
    fig.box(596, 470, 340, 86, "a")
    fig.text(610, 494, "RUN", "mono b s13 ca")
    fig.text(644, 494, "the inner loop · figure 2", "mu")
    fig.lines(610, 514, ["worker call, checks, then review on each part;",
                         "a split counts only if the union passes",
                         "the original packet's checks"])

    fig.arrow("M596 513H541")
    fig.text(568, 505, "outcome", "mu s10 c")
    fig.box(24, 470, 516, 86)
    fig.badge(45, 490, 0, "r")
    fig.text(62, 495, "RUN RECORD", "mono b s13")
    fig.text(152, 495, "one JSON line per attempt", "mu")
    fig.pill(530, 480, "LIVE", "p")
    fig.lines(38, 520, ["label: one_pass · corrected · blocked · failed_checks · failed_ci · no_patch",
                        "correction rounds · tokens per role · seconds · usd_estimate at list prices"], step=17)

    # What each learner takes from the records.
    fig.arrow("M40 470V212H63", "r")
    fig.arrow("M40 296H63", "r")
    fig.arrow("M40 391H63", "r")
    fig.box(64, 176, 476, 72, "r")
    fig.badge(85, 196, 1)
    fig.text(102, 200, "train the predictor", "b s12 cr")
    fig.lines(78, 221, ["gradient-boosted trees on the logged records, scored by Brier and a",
                        "reliability diagram, leaving one repository out at a time"])
    fig.arrow("M405 176V145", "r")

    fig.box(64, 260, 476, 72, "r")
    fig.badge(85, 280, 2)
    fig.text(102, 284, "update the bandit", "b s12 cr")
    fig.text(78, 305, "reward = success − λ · cost", "mono s11")
    fig.text(78, 321, "off-policy evaluation on logged runs (IPS, doubly robust) comes first", "mu")
    fig.arrow("M540 296H568V128H595", "r")

    fig.box(64, 344, 476, 94, "r")
    fig.badge(85, 364, 3)
    fig.text(102, 368, "score the split", "b s12 cr")
    fig.text(78, 389, "reward = one-pass fraction × union passes − α·n − β·cost", "mono s11")
    fig.lines(78, 407, ["a bandit over split prompts: by file, by test, by layer, by size;",
                        "winning splits become few-shot examples, weights only if that stalls"])
    fig.arrow("M540 391H621", "r")

    fig.legend("apr", y=590)
    fig.text(24, 614, "LIVE today: run records and packets derived from merged pull requests.  PLANNED: stages 1 to 3, "
                      "in coding-loop-router/docs/PLAN.md.", "mu s11")
    return fig


FIGURES = {"1-batch-loop": batch_loop, "2-packet-loop": packet_loop,
           "3-call-context": call_context, "4-routing-loop": routing_loop}


def main():
    here = Path(__file__).resolve().parent
    for name, draw in FIGURES.items():
        (here / f"{name}.svg").write_text(draw("light").svg(), encoding="utf-8")
        (here / f"{name}-dark.svg").write_text(draw("dark").svg(), encoding="utf-8")


if __name__ == "__main__":
    main()
