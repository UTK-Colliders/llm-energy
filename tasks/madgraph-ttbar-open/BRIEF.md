# Task brief

Simulate top quark pair production at the LHC, shower it, and show that the
top quark is there.

## Step 1 — generate the hard process

| Property | Required value |
|---|---|
| Process | proton–proton → top–antitop (`p p > t t~`) |
| Perturbative order | leading order |
| Centre-of-mass energy | 13.6 TeV (6800 GeV per beam) |
| Events | 10,000, unweighted |
| Random seed | 42 |
| Format | Les Houches Event file (LHE), gzipped |

Use MadGraph5_aMC@NLO as the generator.

The seed is fixed so that this sample can be compared against ones produced
by other people working from this same brief. Everything in the table is a
requirement on the output; how you satisfy it is yours to work out.

## Step 2 — shower the events

Pass the parton-level events through **Pythia 8** for parton showering and
hadronisation. Let the tops decay.

## Step 3 — reconstruct the top quark

From the showered events, reconstruct the top quark invariant mass and produce
a histogram of it that shows a peak at the top mass (about 173 GeV).

How you reconstruct it is the interesting part of this step and is yours to
decide — which decay channel you target, how you build jets, how you pick
which objects belong to the same top. Combinatorial background is expected;
the peak needs to be visible, not clean.

Sanity check your own result before reporting it: a peak in the wrong place is
a sign the reconstruction is wrong, not a new measurement.

## What to deliver

In this directory:

| File | What it is |
|---|---|
| `unweighted_events.lhe.gz` | the parton-level events from step 1 |
| `top_mass.pdf` | the invariant mass histogram from step 3, as a plot |
| `top_mass_hist.json` | the same histogram as numbers |

The JSON is needed because a plot cannot be checked automatically and the
numbers can. Use exactly this shape, with masses in GeV:

```json
{"bin_edges_gev": [100.0, 105.0, ...], "counts": [3, 11, ...]}
```

`bin_edges_gev` has one more entry than `counts`.

When you are done, report:

- how many events the sample has, and where the peak sits;
- what you did at each of the three steps, briefly — enough that someone could
  repeat it, including which decay channel and jet definition you used;
- roughly how long the generation, showering, and analysis each took, as
  distinct from the time you spent working out what to do;
- anything you are unsure about in the result.

## If you cannot finish

Say so plainly, and say where you got stuck and what you tried. A clear
account of an incomplete attempt is worth more here than a sample you are not
confident in — if you are not sure the events are right, say that rather than
presenting them as correct.

## Working notes

You are on a machine with Docker and network access. This directory is yours;
work in it however you like. Nothing outside it is part of the job.
