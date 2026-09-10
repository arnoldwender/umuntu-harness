# The Proverbs

> The first utterance of the Umuntu Harness: a fixed maxim — the heart of Ubuntu — then a
> rotating *proverb of the day* from African folklore. Each line is carried under the
> name of the people it is attributed to; where the record only supports "African proverb", that is
> what it says. Never "Africa says." Respect means specificity.

**Read the attributions as *attributed*, not as *documented*.** An audit on 2026-09-10 traced
every line in this file, and the honest result is that **three of the thirteen** reach a source
better than a chain of web proverb lists that copy one another. The other ten are marked
`provenance: unverified` in [`sources/`](sources/), each with a note saying exactly what could
not be confirmed — and for four of them the ethnic label found in the best available
ethnographic index points to a **different people or country** than the one printed here.

That is not a reason to delete them; oral folklore is genuinely hard to source, and a proverb
with a thin paper trail is not thereby fake. It is a reason not to let a confident-looking
label stand in for evidence. The per-line detail is in `sources/`, and
[`gate/citations.py`](gate/citations.py) keeps every line here tied to a file that states its
own uncertainty.

## The fixed maxim

```text
"Umuntu ngumuntu ngabantu — a person is a person through other persons."
                                              — Zulu (Nguni), Southern Africa
```

## The rotating proverb

Beneath the maxim, the harness prints one rotating line from [`proverbs.txt`](proverbs.txt) — a
proverb of the day, changing daily. Edit [`proverbs.txt`](proverbs.txt) (one `Proverb — origin`
per line) to curate or extend. The current pool:

> - Rain does not fall on one roof alone — Cameroon
> - A single bracelet does not jingle — Congolese (DR Congo)
> - When spider webs unite, they can tie up a lion — Ethiopia
> - Sticks in a bundle are unbreakable — attributed to the Bondei people, Tanzania
> - The old woman looks after the child to grow its teeth, and the young one in turn looks after the old woman when she loses her teeth — Akan, Ghana & Côte d'Ivoire
> - Little by little fills the measure (Haba na haba hujaza kibaba) — Swahili, East Africa
> - However long the night, the dawn will break — African proverb
> - He who conceals his disease cannot expect to be cured — Ethiopia
> - Knowledge is like a garden: if it is not cultivated, it cannot be harvested — Guinea
> - Wisdom is like a baobab tree; no one individual can embrace it — Akan & Ewe (Ghana, Togo & Benin)
> - However far the stream flows, it never forgets its source — Yoruba, Nigeria

## The discipline epigraphs

Separate from the rotation: [`CODEX.md`](CODEX.md) opens each of the four disciplines with a
proverb. Two of them are also in the pool above; two are not, and appear only there. Listed
here so every quoted line in the repo has one place that accounts for it.

> - *When the roots of a tree begin to decay, the decay spreads to the branches.* — Nigerian proverb — opens **THE COMMONS** (cleanliness). Not in the rotation pool.
> - *Only a fool tests the depth of a river with both feet.* — African proverb — opens **INDABA** (judgment). Not in the rotation pool.
> - *He who conceals his disease cannot expect to be cured.* — Ethiopia — opens **SPEAKING TRUE** (honesty). Also in the pool above.
> - *However long the night, the dawn will break.* — African proverb — opens **HARAMBEE** (persistence). Also in the pool above.

*The harness emits this first, on startup — [`bin/proverb`](bin/proverb).*
