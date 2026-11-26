# LINKS

### [GUI editor](https://nickcoutsos.github.io/keymap-editor/)
### [ZMK docs](https://zmk.dev/docs/keymaps/behaviors/key-press)
### [Keyboard link](https://mechboards.co.uk/collections/pre-builds/products/sofle-wireless-hotswap-kit-pre-build)


# Workflow

1. Edit in [ZMK Studio](https://zmk.studio), then Disconnect.
2. `./scripts/zmk.py sync` to write the live keymap into `config/sofle.keymap`, commit and push.
3. For `.conf` or behavior changes: edit, push, then `./scripts/zmk.py flash left` (or `right`).
