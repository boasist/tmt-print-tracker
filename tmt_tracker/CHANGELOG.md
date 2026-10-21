# Changelog

## 0.3.1

- **Custom plates**: for a plate you combined or changed (say two wheel rims and a bolt cap on one plate), choose
  **custom plate: pick the parts…** in a plate menu, or **change** on a printer card, and give the parts and how many.
  The print counts those parts, the plate is remembered for reprints (and recognised by its preview picture), and the
  queue and planner use it, on the printer it was printed on.
- Fix: a remembered plate is now per printer. Print profiles (MakerWorld's especially) can share a name across
  printers while numbering their plates differently, so one printer's plate 15 could show up as another printer's.
  Existing remembered plates move to the printer they came from.
- A job recognised by a remembered plate or as a reprint is checked again when the printer's preview arrives; if the
  picture clearly shows a different plate, it follows the picture. A stale preview (the previous job's picture at the
  start of a print) is recognised by the picture itself and ignored.

## 0.3.0

- **Print queue per printer**: the plates the planned TMTs still need, each on a printer that has the plate's filament
  loaded, kept balanced and re-planned as prints finish, fail or change. Reorder, move, skip, restore and add plates;
  set what filament a printer can print; turn a printer's queue off. A failed plate is retried first.
- **Phone alerts** (option `notify`): when a print ends, the result, the TMT's progress and the next plate, with a
  **Plate is clear** button. Option `alert_url` for where tapping it goes.
- **Printer details**: click a printer for its camera (WebRTC or HLS through Home Assistant, else a picture every
  2 seconds), printer alerts, the print, temperatures, fans, filament and AMS units, and nozzles.
- **Print times per printer**: every print's slicer estimate and real printing time (paused time left out), with the
  filament and nozzle. Shown on the queue with totals; edit, exclude, delete or add samples on the planner page.
- **Build planner** (new page): each printer's setups in order (nozzle size, filament, part groups), your hours, and a
  plan that keeps the printers busy, with a timeline and a "when you're needed" list. **Use this plan** sets the
  queues, including when to swap the nozzle.
- Profiles: `printer_models` on a plate rule keeps a plate on matching printers (the single-piece push handle on H2
  printers). The catalog records each plate's model volume for rough time guesses.
- Wide tables scroll on a phone instead of widening the page.


## 0.1.2

- Printer cards show when the print will be done ("Done at 4:49 PM", with the weekday if it isn't today) instead of
  how the plate was recognised. Finished prints show "Finished at …"; plates outside the build keep a "not a TMT plate" tag.

## 0.1.1

- Fix: a new print could be recognised as the previous plate. When a job starts, the printer updates its preview
  image's timestamp before the new picture arrives; a match to the previous job's plate on a different plate number is
  now ignored until the real picture comes.
- A newer preview during a job that was recognised by image is checked again, and the job follows it if it clearly
  shows a different plate.

## 0.1.0

First release.

- Finds Bambu Lab printers in Home Assistant automatically.
- Builds the TMT parts list from your uploaded 3MF files and a build profile (MakeGood TMT Rev C built in).
- Recognises the plate being printed by job name + plate number, or by the plate preview image.
- Counts parts when a print finishes; failed prints don't count. Manual corrections, undo, assign, import.
- Per-TMT progress and "mark assembled".
- Dashboard in the sidebar through ingress.
