# TMT Print Tracker for Home Assistant

A Home Assistant app (add-on) for volunteer makers printing **Toddler Mobility Trainers (TMTs)**: the free, 3D-printed
mobility trainers for children organised by [3D Mobility](https://3d-mobility.org) and MakeGood.

It watches your **Bambu Lab** printers and keeps the parts list for you:

- Lists every part a TMT needs (about 100 pieces), grouped the way you assemble them, with what's printed for each TMT
  you're building.
- Works out which plate of which design file each printer is printing — from the job name and plate number, or by
  comparing the printer's plate preview with your files' plate previews (so prints sent from MakerWorld or Bambu Handy
  are recognised too).
- Counts a plate's parts when the print finishes. Failed prints don't count; you can correct anything by hand.
- Shows each TMT's progress, and takes a TMT's parts out of the count when you mark it assembled.

## What you need

- Home Assistant OS or Supervised (apps/add-ons).
- Your Bambu Lab printers in Home Assistant through the
  [Bambu Lab integration](https://github.com/greghesp/ha-bambulab) (HACS).
- The TMT design files (`.3mf`) you print from. **They are not included here**: they belong to their designers.
  Download them as a maker, then upload them on the app's page.

## Install

1. In Home Assistant: **Settings → Apps (Add-ons) → App store → ⋮ → Repositories**, add
   `https://github.com/boasist/tmt-print-tracker`.
2. Install **TMT Print Tracker**, start it, and turn on **Show in sidebar**.
3. Open **TMT** in the sidebar and upload your 3MF files under **Files**. The page lists any design file the build
   profile still needs.

Your printers are found automatically. See [the documentation](tmt_tracker/DOCS.md) for options and for changing the
build (which plates count, quantities, either/or parts).

## The default build

The built-in profile follows MakeGood's 3D Toddler Mobility Trainer, Rev C, with these choices: one-piece footrest,
Rev B wheel rims (4, one per plate), 16 TPU tires from the 0.8 mm HF tire file, the single-piece push handle (H2-series
printers) and V2 push handle grip, the separate harness & straps file, kit caster mounts, and the headrest as optional.
Copy and edit the profile to build it differently.

## Credits

TMT designs: MakeGood and the 3D Mobility maker community (including the contributors of the harness & straps, tire,
push handle grip and single-piece handle variants). This project only tracks prints; it isn't affiliated with
MakeGood, 3D Mobility or Bambu Lab.

## License

MIT for the code in this repository. The TMT design files keep their own licenses.
