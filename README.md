# RexTuul

Zero-dependency PNG <-> REXPaint (.xp) batch converter and TUI viewer.

## Usage

### Convert
```bash
python3 rextuul.py ./dir/          # PNG -> XP [P2X]
python3 rextuul.py ./dir/ -x       # XP -> PNG [X2P]
```

### View
```bash
python3 rextuul.py ./dir/ --watch
```

- **Dithering**: Use `-f` for Floyd-Steinberg or `-d [0-5]` for Bayer.
- **Portability**: Single file, standard library only. No Pillow required.
