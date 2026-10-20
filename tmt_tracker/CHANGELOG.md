# Changelog

## 0.3.1

- Brings in 0.3.0 (print queue, phone alerts, printer details, print times, build planner) on top of the Moonraker,
  projects and custom plate changes below. Moonraker printers take part in the queue and the planner too.

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


## 0.2.7

- A project's parts list: **all printed** (parts printed before the tracker knew about the project) and **take N from
  spares** per part, next to − / +. **New project** can take what it needs from the spares right away.

## 0.2.6

- **Custom plate** under "which plate is this?": for a modified plate that isn't in any file, pick the parts and how
  many (4 tires, say). It counts like a plate and is remembered for reprints of the same job.

## 0.2.5

- Printers show the name you give the device in Home Assistant, else the device's own name (0.2.4 showed bare serial
  numbers for Bambu Lab printers).

## 0.2.3

- Printer cards fit their content: long printer and job names are cut short (full text on hover), menus stay
  inside the card, and a recognised plate shows its plate name and number.

## 0.2.2

- After an upload, Rescan or removing a file, the printers' current jobs are looked at again, so a print that
  started before its file was uploaded is recognised.

## 0.2.1

- Uploads go in 8 MB pieces, so 3MF files over 16 MB (Home Assistant's ingress limit) upload; sliced projects are
  often 100 MB.

## 0.2.0

- Projects: keep a request, a chair not yet for a child, or a parts order (e.g. wheels for another maker) separate.
  Each printer card has **Printing for**: finished plates count toward that project instead of the spare parts. A
  project is a whole TMT or a parts order with your own quantities; its card shows progress and what's still needed,
  can take parts from the spares, and is marked done when assembled or shipped. Move a print to another project in
  Recent prints. Without projects everything works as before.
- A project's card lists its parts with − / + to correct them (a part that failed on a plate that otherwise
  finished), and **print list** prints a checklist of what's printed and what's left.
- **Not for a TMT**: choose it under Printing for (or on an unrecognised print) for prints that aren't TMT parts; they
  are logged but never counted or asked about.
- Moonraker printers (Snapmaker U1 and other Klipper printers) through the
  [Moonraker integration](https://github.com/marcolivierarsenault/moonraker): found automatically next to Bambu Lab
  printers, with live status, progress, time left and layers, and their print thumbnail as the plate preview.
- A job is also recognised when it is named after the plate or the one part on it, as Orca-style slicers name their
  output files (`3DTMT F SEAT BACK CENTER TOP REV A_PETG_9h44m.gcode`). Klipper printers send no plate number.
- Built-in profile `snapmaker_u1_complete_chair` for the Snapmaker U1 "Complete Chair" project (31 plates, harness,
  cushions, tires and the one-piece push handle in one file).
- A profile's file can also match by file name (`filename:` pattern), for files that share a Title.
- The `printers` option takes an optional `type` (`bambu` or `moonraker`).
- A plate of a file the profile doesn't name counts when every part on it is a part of the build, so the same parts
  laid out for another printer (a Snapmaker U1 project, say) count without editing the profile. Turn it off with
  `count_other_files: false` in the profile.

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
