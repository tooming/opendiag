# VAG Battery-Replacement Coding — Research

Reference for `vag_battery.py` (Octavia Mk3 / MQB battery coding). Written
after replacing the OEM battery with an AGM unit and needing to code that
into the car — same "sourced, gated, never guess a write" policy used for
the BMW DME adaptation-erase opcode in `ADAPTATIONS.md`.

## Why AGM specifically needs coding

MQB-platform cars (Octavia Mk3 included) track the fitted battery's
capacity and technology in the Gateway so the BMS applies the right charge
profile, decides whether start-stop/regen braking are available, and knows
when the battery is due for a "new battery" relearn. AGM tolerates (and
needs) a different charge-voltage curve than flooded/EFB; leaving the old
battery's data coded in after an AGM swap is widely reported to suppress
start-stop, throw "check battery" warnings, and undercharge the new
battery over time. This is exactly the kind of write `transaction.py`
exists for: read current state → backup → write → verify.

## What's publicly confirmed

- **Module**: Gateway, VCDS diagnostic address `19` (J533), UDS. NOT the
  DME/LCM path BMW uses for the equivalent function, and NOT a separate
  "Battery Regulation" module at address `61` — that address exists on
  some other VAG platforms with an inline intelligent battery sensor, but
  the community-documented Octavia Mk3/MQB procedure lives on the Gateway.
  (Independently corroborated: `PyVCDS`'s own module table also has `0x61`
  labelled "Battery" but with zero implementation behind it — a different,
  unrelated address, not a competing answer for this platform.)
- **Function name**: "Battery — replacement: adaptation", listed under the
  Gateway's Adaptation service in VCDS-label databases.
- **Logical fields** (VCDS/vis4vag channel labels): Rated battery capacity
  (Ah), Battery technology (AGM vs. flooded/EFB), Battery manufacturer,
  Battery serial number.
- **Technology selector semantics**: VCDS distinguishes the two by battery
  case appearance — "Vlies" (fleece) = AGM, "Nass" (wet) = flooded/EFB.
- **Manufacturer codes** (informational, from a public vis4vag summary):
  3-letter codes such as `VAX`=Varta, `UMX`=Akuma, `BAX`=Banner,
  `MLA`=Moll, `TUX`=Exide, `JCB`=Clarios/Johnson Controls, `XDO`=Boading.
- **The serial-number field is what actually triggers the relearn**:
  multiple independent write-ups agree that changing even one digit of the
  stored serial (vs. entering the real new battery's serial) is what tells
  the BMS "new battery installed" — capacity/technology matter for
  correctness but the serial bump is the trigger.
- **Practical guidance from owners**: if the replacement battery is the
  same *technology* as the original (e.g. flooded→flooded), the technology
  channel doesn't need touching — only capacity + serial. An EFB/flooded →
  AGM swap (this project's case) does need the technology channel changed.

Sources (all fetched via search snippets during this research; the live
pages themselves 403'd from this sandbox's network egress — treat as
corroborating, re-verify directly before trusting blindly):
- vis4vag diagnostics database, "VW / Up (AA) / 19 - Gateway GW MQB
  (EV_GatewLear) UDS / Battery - replacement: adaptation" (and the
  equivalent Škoda/other-MQB pages under the same site) — names the four
  channels and the manufacturer-code table; the underlying raw UDS DIDs
  are paywalled and stated to be scoped per vehicle/gateway-software
  variant, i.e. not a single global constant even if paid for.
- Ross-Tech Wiki, "Battery Replacement" — general VAG battery-registration
  background (page itself blocked from this sandbox; referenced via search
  index only).
- BRISKODA forum, "Idiots guide to coding for new battery in VCDS" (Octavia
  Mk3 subforum) and "Replacing main car battery might require CAN-Gateway
  coding" — owner-documented VCDS walkthroughs for this exact car.
- OBDeleven support article, "How to register (code) a new car battery" —
  consumer-tool documentation of the same underlying adaptation.
- `PyVCDS` (github.com/baconwaifu/PyVCDS), `vw.py` — independent
  confirmation that address `0x61` is labelled "Battery" in VCDS's module
  table (a different address than the Gateway path used here; included for
  completeness, not as the answer).

## What's NOT confirmed — the gate

The raw UDS data identifiers (DIDs) the four channels above map to, and
whether `SecurityAccess` (UDS `0x27`, VAG's own proprietary seed/key
algorithm) is required before the Gateway will accept a write, are **not
public**. vis4vag's own paid database keeps the numeric values behind a
paywall and explicitly scopes them per vehicle **and** per Gateway
software variant — meaning even a number pulled from a different Octavia
isn't guaranteed to match this one's Gateway part/software revision.

Per this project's standing policy (see `ADAPTATIONS.md`): **never guess a
write value and send it to the car.** The Gateway is a much higher-blast-
radius module than a BMW DME — it also carries central locking, lighting,
and comfort-CAN routing logic — so a wrong `WriteDataByIdentifier` here is
a worse mistake than the BMW case, not a better-odds one.

`vag_battery.CHANNEL_SPECS[*]["did"]` is therefore `None` for all four
fields. `build_write_plan()`/`code_battery()` refuse to write to a real
car until every needed DID is filled in. `--demo` runs the full
read→backup→write→verify pipeline against a simulated Gateway so the
plumbing is exercisable and testable without that data.

## Recommended path to un-gate

1. Perform the battery coding once via VCDS or OBDeleven (see "Manual
   procedure" below) with logging turned on — VCDS's "Diagnostic Log" /
   OBDeleven's export both capture the raw UDS requests/responses.
2. From that trace, identify the `WriteDataByIdentifier` (`0x2E`) requests
   sent while setting each channel: the 2-byte DID and the data bytes for
   at least one known-good value (e.g. the capacity you actually entered).
3. Fill in `CHANNEL_SPECS[<field>]["did"]` in `vag_battery.py` with the
   confirmed DID(s), and correct `["encode"]` to match the real byte
   layout observed (this file's current encoders are placeholders, not
   wire-confirmed).
4. Note whether a `SecurityAccess` login preceded the writes in the trace;
   if so, `code_battery()`'s `write_fn` needs a `adapter.uds.security_access(...)`
   call added before the writes, with a real seed/key function — do not
   guess the algorithm.
5. Re-run `test_vag_battery.py`, then test `code-battery` (without
   `--demo`) once — watch the result, same as the BMW adaptation-erase
   policy of treating the first real run as the functional proof.

## Manual procedure (available today, no gate)

Until the above is done, use VCDS or OBDeleven directly — this is the same
information `vag_battery.describe_manual_procedure()` prints from the CLI:

1. Select control module **19 - CAN Gateway**.
2. Open **Adaptation** (channel-based, not the separate "Coding" 0x07
   long-coding string).
3. Find the **"Battery - replacement: adaptation"** channel group and set:
   - **Rated battery capacity** — the new battery's Ah rating.
   - **Battery technology** — AGM ("Vlies"/fleece case) vs. flooded/EFB
     ("Nass"/wet case). Pick AGM for this swap.
   - **Battery manufacturer** (if offered) — match the new battery.
   - **Battery serial number** — change at least one digit from the old
     stored value; this is what actually signals the relearn.
4. Save/apply each channel, then clear any faults the Gateway raised while
   the values were mismatched.

## Open questions for the user / next agent

- Access to VCDS or OBDeleven with a diagnostic/UDS log/export feature, to
  capture the one trace that removes all the DID guesswork?
- Confirm this Octavia's Gateway part number / software variant, in case
  future confirmed DIDs need to be tied to a specific variant rather than
  treated as a project-wide constant.
