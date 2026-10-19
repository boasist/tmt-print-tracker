# Changelog

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
