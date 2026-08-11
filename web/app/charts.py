"""Turning figures into the numbers an SVG needs.

Kept out of the templates because a template that computes stroke offsets is a
template nobody can check, and out of the repos because none of this is a question
about the database. It is arithmetic with one job: give the markup a list of
segments it can render without thinking.
"""

from __future__ import annotations

from decimal import Decimal

# The categorical slots, stepped for this application's dark surface (#171a21) and
# validated against it rather than assumed: all six clear the lightness band, the
# chroma floor, the adjacent-pair CVD gate (worst ΔE 8.4) and 3:1 contrast.
#
# Six is the ceiling and the ordering is the safety mechanism, not decoration — the
# gates are between *neighbouring* segments, which is what a ring has. Anything past
# the sixth category folds into «Այլ» in grey rather than inventing a seventh hue: a
# generated one is indistinguishable from an existing one to a colourblind reader and
# would fail every check.
SLOTS = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300")
REST = "#6b7280"
MAX_SLICES = 6
OTHER = "Այլ"

# A circle of circumference 100, so a segment's length in the dash array *is* its
# percentage and nothing has to be scaled twice.
RADIUS = Decimal("15.9155")
# The surface-coloured gap that separates touching segments. Two pixels at this
# radius, expressed in the same units as the dash array.
GAP = Decimal("0.9")


def donut(rows, *, limit: int = MAX_SLICES) -> dict:
    """Segments for a part-to-whole ring, largest first.

    Each row needs ``category`` and ``total``. Returns the total, and one entry per
    segment carrying its colour, its share, and the dash geometry to draw it — plus
    the figure and percentage, because the legend beside the ring is what makes the
    thing readable to somebody who cannot tell two of the colours apart.

    Every segment also carries ``members``: the categories it stands for. One for an
    ordinary slice, and for «Այլ» the whole folded tail — so the slice that is not a
    category can still be asked what it is made of, rather than being the one part of
    the ring that does not answer.
    """
    counted = [
        (str(row["category"]), Decimal(row["total"]))
        for row in rows
        if Decimal(row["total"]) > 0
    ]
    total = sum((amount for _, amount in counted), Decimal(0))
    if not counted or total <= 0:
        return {"total": Decimal(0), "segments": []}

    counted.sort(key=lambda pair: pair[1], reverse=True)
    # (label, amount, the categories it stands for) — positional rather than keyed by
    # name, because an owner is entitled to call one of their own categories «Այլ» and
    # a dict would then hand the folded tail's membership to it.
    slices = [(name, amount, [name]) for name, amount in counted]
    if len(slices) > limit:
        head, tail = slices[: limit - 1], slices[limit - 1 :]
        slices = [*head, (
            OTHER,
            sum((amount for _, amount, _ in tail), Decimal(0)),
            [name for name, _, _ in tail],
        )]

    segments = []
    offset = Decimal(0)
    for index, (name, amount, members) in enumerate(slices):
        share = amount / total * 100
        # The gap is taken out of the drawn arc rather than added between arcs, so the
        # shares still sum to the whole circle and a rounding error cannot open a seam.
        drawn = max(Decimal("0.1"), share - GAP)
        segments.append({
            "label": name,
            "members": members,
            "amount": amount,
            "share": share,
            # Grey only for the fold itself — a category of the owner's own called
            # «Այլ» is a category like any other and keeps its slot colour.
            "colour": REST if len(members) > 1 else SLOTS[index],
            "dash": f"{drawn:.3f} {100 - drawn:.3f}",
            # SVG runs the dash offset backwards, so a segment that should begin at
            # `offset` round the ring is drawn from minus that.
            "offset": f"{-offset:.3f}",
        })
        offset += share
    return {"total": total, "segments": segments}
