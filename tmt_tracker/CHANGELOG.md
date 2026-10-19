# Changelog

## 0.2.0

- Moonraker printers (Snapmaker U1 and other Klipper printers) through the
  [Moonraker integration](https://github.com/marcolivierarsenault/moonraker): found automatically next to Bambu Lab
  printers, with live status, progress, time left and layers, and their print thumbnail as the plate preview.
- A job is also recognised when it is named after the plate or the one part on it, as Orca-style slicers name their
  output files (`3DTMT F SEAT BACK CENTER TOP REV A_PETG_9h44m.gcode`). Klipper printers send no plate number.
- The `printers` option takes an optional `type` (`bambu` or `moonraker`).

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
