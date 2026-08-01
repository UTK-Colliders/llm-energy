# Task brief

Produce a simulated sample of top quark pair production at the LHC.

## What the sample must be

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

## What to deliver

Leave the gzipped LHE file at `unweighted_events.lhe.gz` in this directory.

When you are done, report:

- where the file is and how many events it contains;
- what you did to produce it, briefly — enough that someone could repeat it;
- roughly how long the generation itself took, as distinct from the time you
  spent working out what to do;
- anything you are unsure about in the result.

## If you cannot finish

Say so plainly, and say where you got stuck and what you tried. A clear
account of an incomplete attempt is worth more here than a sample you are not
confident in — if you are not sure the events are right, say that rather than
presenting them as correct.

## Working notes

You are on a machine with Docker and network access. This directory is yours;
work in it however you like. Nothing outside it is part of the job.
