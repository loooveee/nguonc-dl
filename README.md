# nguonc-dl

**English** · [Tiếng Việt](README.vi.md)

> [!WARNING]
> **This project is for educational purposes only.** It shows how HLS streaming, stream decryption
> and parallel downloads work. Do not use it to re-upload, share or sell movies.

Download movies and series from **phim.nguonc.com** as **MP4** files.

- Paste a link: a movie downloads as one file, a series downloads **every episode**
- Always downloads the **highest quality** the site has (the real resolution is checked, e.g. `1080p (1920x960)`)
- See the **quality, size and bitrate** before downloading (`--info`)
- Fast downloads with **aria2c** (falls back to yt-dlp)
- Removes the **ad segments** spliced into the movie, and downloads broken segments again
- Live progress bars: one per episode plus a total, with speed and ETA
- Stop anytime, run the same command again to continue

## Install

1. Install [Python 3.10+](https://www.python.org/downloads/)
   (on Windows, tick **Add python.exe to PATH** on the first screen of the installer)
2. Download this project: **Code** → **Download ZIP**, then unzip it
3. Open a terminal in the unzipped folder (on Windows: click the address bar of the folder,
   type `cmd` and press Enter), then run:
   ```bash
   python -m pip install -r requirements.txt
   ```
4. Put `aria2c.exe` next to `nguonc-dl.py` (recommended, several times faster)
   - aria2c: https://github.com/aria2/aria2/releases (the `win-64bit` zip, take `aria2c.exe` out of it)
   - ffmpeg is installed by step 3. You can also put your own `ffmpeg.exe` next to the script.

## Use

```bash
python nguonc-dl.py "https://phim.nguonc.com/phim/movie-name"
```

| Command | What it does |
|---|---|
| `python nguonc-dl.py <link>` | Download the movie, or every episode of a series |
| `python nguonc-dl.py movie-name` | The name from the link works too |
| `python nguonc-dl.py <link> --info` | Only show the quality, size and bitrate |
| `python nguonc-dl.py <link> -e 1-5,8` | Only episodes 1 to 5 and 8 (`10-` = from 10 to the end) |
| `python nguonc-dl.py <link> -q 720` | Pick a lower quality, when the movie has several (`best`, `worst`, `1080`, `720`, ...) |
| `python nguonc-dl.py <link> -s 2` | Use server 2 (e.g. dubbed instead of subtitled) |
| `python nguonc-dl.py <link> --list` | Only list the servers and episodes |
| `python nguonc-dl.py <link> -o D:\Movies` | Save to another folder |
| `python nguonc-dl.py <link> -j 3` | Download 3 episodes at a time (default 2) |
| `python nguonc-dl.py <link> --lang vi` | Show messages in Vietnamese |

Files are saved in `downloads/<title> (<year>)/`, named like `Movie.Name.2024.mp4` for a movie
and `Series.Name.S01E01.mp4` for a series.

Check before downloading:

```
  Quality           Size          Bitrate     Length
  ──────────────────────────────────────────────────
  1080p (1920x960)  ~1.7 GB       2.1 Mbps    1:56:53  ← downloaded by default
  This is the only quality the site has for this movie.
```

The highest quality is picked by default. Most movies on the site have a single quality
(usually 1080p); when there are several, all of them are listed and `-q` picks another one.

While downloading:

```
  ⠹ Ep 01       ━━━━━━━━━━━━━━━━━╸━━━━━━━━━━━━    58.3%    392.9 MB / 673.3 MB     17.4 MB/s   ETA   00:16
  ⠼ Ep 02       ━━━━━━╸━━━━━━━━━━━━━━━━━━━━━━━    21.0%    141.1 MB / 674.2 MB     17.1 MB/s   ETA   00:31
  ───────────────────────────────────────────────────────────────────────────────────────────────────────
  Σ Total 1/4   ━━━━━━━━━━━━━╸━━━━━━━━━━━━━━━━    44.8%      1.2 GB / 2.7 GB       34.4 MB/s   ETA   00:44
```

Messages are in **English** by default (Vietnamese if your system language is Vietnamese).
Use `--lang en` or `--lang vi` to choose. Use `--no-color` (or set `NO_COLOR=1`) for plain text.

Run `python nguonc-dl.py -h` to see all options.

## Failed downloads

If an episode can't be downloaded, it is shown in red with the reason, and listed in **`failed.txt`**
in the movie folder (episode, link and reason). Run the same command again to retry: finished
episodes are skipped and unfinished ones continue where they stopped. `failed.txt` is removed once
nothing fails.

## Problems?

- **`python` is not recognized:** reinstall Python and tick **Add python.exe to PATH**
- **"Missing library":** run `python -m pip install -r requirements.txt` again
- **"rate limited by the server":** the site allows about 20 links per hour. Short waits are done
  automatically; for longer ones, wait a while and run the same command again (or use `-e` for fewer episodes)
- **"HTTP/2 is needed" / HTTP 403:** run `python -m pip install "httpx[http2]"`
- **Slow downloads:** put `aria2c.exe` next to `nguonc-dl.py`
- **Ctrl+C:** stops right away. Run the same command again to continue

## Contributing

Bug reports and pull requests are welcome. Changes that get past captchas or other protections
will not be accepted.

- **Report a bug:** open an [issue](https://github.com/loooveee/nguonc-dl/issues) with the command
  you ran, the error text (or `failed.txt`), your OS and `python --version`.
- **Send a fix:** fork the repo, create a branch, commit, push it to your fork, then open a pull request.
  Say what you changed, why, and how you tested it.
- **Rules:**
  - Keep everything in `nguonc-dl.py`. The docstring at the top has a code map.
  - Write every message in both languages: `t("English", "Tiếng Việt")`.
  - Use the color helpers (`green()`, `red()`...) instead of raw ANSI codes.

## Disclaimer

**This is for educational purposes only.** You are responsible for how you use it.
Respect copyright and the website's terms of use.

This project is not affiliated with phim.nguonc.com. The author is not responsible for any misuse.

## License

MIT © loooveee
