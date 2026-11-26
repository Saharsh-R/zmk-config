# zmk-config

ZMK firmware config for a wireless Sofle (nice!nano v2). The left half is the central: it holds the keymap and runs ZMK Studio. The right half only reports key positions.

## Workflow

- Edit keys live in ZMK Studio (https://zmk.studio, Chrome, USB). Changes are stored on the keyboard, not in this repo.
- Click Disconnect in Studio, then run `./scripts/zmk.py sync`. It reads the live keymap over serial, rewrites the `bindings` blocks in `config/sofle.keymap` (keeping existing spellings like `LCMD`, `&lt (2)`), commits and pushes. Use `--dry-run` to preview.
- Changes Studio can't make (`sofle.conf`, new behaviors or macros, ZMK updates) go in the files directly. Push, then `./scripts/zmk.py flash left` (and `right` if the change affects it). It waits for CI, downloads the firmware and flashes over the bootloader's serial port.
- After flashing a file edit, Studio's stored keymap still wins over the file. Sync first, then use "Restore Stock Settings" in Studio if the file should take over.

## Gotchas

- The work laptop blocks USB drives, so the `NICENANO` drive never mounts. Always flash with `zmk.py flash` (serial DFU via `adafruit-nrfutil`).
- Only one program can hold the serial port: Studio or `zmk.py sync`, not both.
- Studio unlock: hold the outermost left thumb key (adjust layer) and press Y.
- Layer numbers in the file are positions; Studio's layer ids match them only until layers are reordered in Studio.
- Push over HTTPS. The keychain token is a fine-grained PAT that must include this repo.
