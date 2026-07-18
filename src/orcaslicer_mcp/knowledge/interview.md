---
topics: [interview, fundamentals, context, questions]
orca_keys: []
---

# Interview protocol

## The seven fundamentals

Before reasoning about any of the considerations files (strength,
surface-quality, dimensional-accuracy, speed-vs-quality,
material-environment, large-nozzle, small-features), a print decision
needs a minimum set of facts about the situation. These seven
fundamentals are the complete list — every consideration mapping in
this knowledge base ultimately reads one or more of them back out of
the user's situation:

1. **Purpose** — what the part is actually for (display piece,
   functional mechanism, fit-check, gift, replacement part). Purpose is
   the fundamental that determines how much the others matter at all: a
   decorative print has a much shorter list of material considerations
   than a load-bearing one.
2. **Mechanical load + direction** — whether the part is loaded at all,
   how much, and along which axis relative to the layer stack (in-plane
   bending vs. Z-axis pull, static vs. repeated/cyclic). This is what
   `strength.md` reads to decide between wall count, infill pattern, and
   part orientation.
3. **Fit** — whether the part has to mate with something else (a
   bolt, another printed part, an existing physical object) and how
   tight that tolerance needs to be. This is what `dimensional-accuracy.md`
   reads to decide whether a test coupon is warranted.
4. **Environment** — where the part will live and what it's exposed to
   (heat, sun/UV, moisture, outdoors, a car). This is what
   `material-environment.md` reads to choose between PLA, PETG, and ASA.
5. **Appearance** — how much the visible finish matters (handled and
   inspected closely vs. hidden inside an enclosure vs. thrown away
   after one use). This is what `surface-quality.md` and
   `speed-vs-quality.md` read to decide how much outer-wall/top-surface
   speed to spend on cosmetics.
6. **Budget** — how much print time (and, secondarily, material) is
   available or acceptable. This is what `speed-vs-quality.md` and
   `large-nozzle.md` read to decide how aggressively to trade time
   against quality.
7. **Hardware truth** — what the actual printer, nozzle, and material on
   hand are (nozzle diameter, drive type, filament in the spool right
   now) as opposed to what's assumed or ideal. This grounds every
   consideration file's specific key recommendations in what's
   physically achievable on this setup, not a generic one.

## Behavioral rule

Derive silently what the user already answered. Ask ONLY about missing
fundamentals material to THIS decision — one or two questions,
conversational, never a form. If nothing material is missing, ask
nothing. Persist answers with `remember()` so no user states a
fundamental twice.

In practice this means: read the user's own description of the part,
its use, and their setup first, and only turn a fundamental into an
actual question when the decision at hand can't proceed without it —
e.g. don't ask about environment when the user already said "this sits
on my desk," and don't ask about mechanical load when the part is
already described as a purely decorative figurine. When a question is
needed, ask it the way a person would in conversation, not as a
numbered checklist running through all seven fundamentals at once —
surfacing two unrelated questions back to back reads as an interrogation
rather than a conversation, and it's also unnecessary: only the
fundamentals the current decision actually depends on are missing-and-material,
and everything else can stay unasked. Once an answer is given, store it
via `remember()` so a later question in the same or a future session
doesn't force the user to repeat information already on record.

## Context -> consideration mappings

- IF the user's situation already states purpose, load, fit, environment, appearance, budget, and hardware truth THEN weigh asking nothing further, because all seven fundamentals are already derivable from what's been said.
- IF the user's situation describes geometry or a request but is missing mechanical load + direction and the decision at hand is about wall count, infill, or orientation THEN weigh asking one conversational question about how the part will be loaded, because `strength.md` cannot be applied without it.
- IF the user's situation is missing environment and the decision at hand is about material choice THEN weigh asking one question about where the part will live or what it's exposed to, because `material-environment.md` cannot recommend a filament without it.
- IF the user's situation is missing fit and the part has any hole, peg, thread, or mating feature mentioned THEN weigh asking whether it needs to mate with something and how tightly, because `dimensional-accuracy.md`'s compensation and coupon-test guidance depends on it.
- IF the user's situation already implies budget or appearance from context (e.g. "just a quick test" implies low budget-for-time and low appearance priority) THEN weigh deriving those fundamentals silently rather than asking, because they're already answered in substance even if not in the exact fundamental's vocabulary.
- IF a fundamental was already given earlier in this session or a prior one THEN weigh retrieving it via memory rather than asking again, because `remember()` exists precisely so fundamentals are captured once.
