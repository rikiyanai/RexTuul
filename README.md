# RexTuul

![RexTuul Demo](demo.gif)

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






### Custom Dither Intensity
```bash
python3 rextuul.py ./dir/ -d 0   # flat colors, no dither
python3 rextuul.py ./dir/ -d 3   # medium dither
python3 rextuul.py ./dir/ -d 5   # maximum dither
```

### Single File
```bash
python3 rextuul.py my_image.png
```

### Force Specific Width
```bash
python3 rextuul.py ./dir/ -W 80
```

### Watch Mode Keys
| Key | Action |
|-----|--------|
| `↑` / `k` | Scroll up |
| `↓` / `j` | Scroll down |
| `PgUp` / `PgDn` | Scroll by page |
| `g` / `G` | Jump to top / bottom |
| `q` / `Ctrl+C` | Quit |
