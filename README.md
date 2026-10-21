# ororo

A dependency-free command-line tool for downloading movies and shows available
through an ORORO subscription, with quality limits and WebVTT subtitles.

Requires Python 3.10+ on Linux or macOS. HLS downloads need `ffmpeg`; inspecting
existing files for quality changes needs `ffprobe` (usually installed with ffmpeg).

## Install

```sh
pipx install git+https://github.com/levmv/ororo
```

Update with `pipx reinstall ororo`. `uv tool install git+https://github.com/levmv/ororo`
works the same way.

The tool is a single file, so it can also be copied onto the PATH without pip:

```sh
install -D -m 755 ororo.py ~/.local/bin/ororo
```

Installation does not need sudo. The examples below use the installed command;
use `./ororo.py` when running from a checkout.

## Quick start

```sh
ororo login
ororo search "the martian" --type movies
ororo info https://ororo.tv/en/movies/the-martian
ororo https://ororo.tv/en/movies/the-martian -o /path/to/archive
```

## Commands

| Command | Purpose |
| --- | --- |
| `ororo URL ...` | Download movies or shows |
| `ororo login` | Verify and save account credentials |
| `ororo search QUERY` | Search titles, optionally including a year |
| `ororo info URL ...` | Show descriptions and available episodes |
| `ororo --help` | List options and examples |
| `ororo --version` | Show the installed version |

Options may appear before or after positional arguments. Search accepts
`--type movies`, `--type shows`, or `--type all` (the default).
Info accepts the same season/episode filters and input lists as downloads.
Download-specific options are rejected for commands where they have no effect.

## Credentials

`ororo login` asks for the login and password in a terminal, verifies them,
then saves `~/.config/ororo/credentials.json` with mode `0600`. The newly
created `ororo` configuration directory has mode `0700`. If
`XDG_CONFIG_HOME` is set to an absolute path, it replaces `~/.config`.

Normal commands use credentials in this order:

1. `--user` overrides the login from `ORORO_USER`.
2. `ORORO_PASSWORD` overrides the saved password.
3. Missing values are read from the credentials file. A saved password is used
   only when its saved login matches the selected login.
4. Missing values are requested interactively if stdin is a terminal.

A complete login/password supplied through the command line/environment
bypasses the saved file, even if that file is damaged. There is no password
command-line argument. Environment credentials are not saved by normal commands;
`ororo login` explicitly verifies and saves them.

Use `--credentials-file FILE` to select another credentials file.

Rejected passwords produce an error suggesting `ororo login`. Use
`ororo login --user you@example.com` to replace a damaged saved file.
Delete the credentials file to forget an account.

`--non-interactive` and `--json` never prompt. Unattended commands without
valid credentials fail with a readable error and a nonzero exit status.
Environment variables may be visible to other processes under the same user.

## Settings

Optional defaults are read from `~/.config/ororo/config.json` (respecting
`XDG_CONFIG_HOME`). For example:

```json
{
  "output": "/srv/media",
  "max_quality": "1080p",
  "subs": "en,ru",
  "delay": 1
}
```

Use `--config FILE` to select another file. An explicitly selected file must
exist. Unknown keys and invalid values produce an error.

Command-line options override settings. Without settings the output is the
current directory, quality is `best`, subtitles are `en,ru`, and the delay
between videos is one second. Relative output paths are relative to the current
working directory; use an absolute path for unattended runs.

## Downloads and previews

Preview one episode:

```sh
ororo https://ororo.tv/en/shows/heroes --season 1 --episode 1 --dry-run
```

Download selected seasons with all available subtitle languages:

```sh
ororo https://ororo.tv/en/shows/heroes \
  --season 1,3-4 --subs all -o /path/to/archive
```

`--episode 1-3` selects those episode numbers within every selected season.
Use `--subs none` to disable subtitles. Missing requested languages produce
warnings; available requested languages are still downloaded.

`--max-quality 720p` means the highest available version up to 720p.
Supported limits are 240p, 360p, 480p, 720p, 1080p, 1440p, and 2160p;
`best` has no ceiling.

Movies and shows can be mixed in one command. Use `-i` / `--input-file` for
UTF-8 lists with one URL per line. Blank lines and lines starting with `#`
are ignored. URL arguments are processed first, then file entries; duplicate
pages are ignored. All input URLs are validated before contacting ORORO.

```sh
ororo -i list.txt -o /path/to/archive --dry-run
ororo -i list.txt -o /path/to/archive --keep-going
```

A preview uses the same per-file plan as downloading and shows:

- the selected quality and whether the file will be downloaded, skipped, or upgraded;
- final video size and remaining video traffic, accounting for resumable partial files;
- peak additional free space for sequential downloads, including quality replacements.

HLS sizes are estimates and unknown sizes remain unknown. Subtitle sizes and
small metadata files are excluded. Previews contact ORORO but do not create
download directories, locks, or media files. Authentication entered interactively
may still be verified and saved.

The default stops on the first failed item. `--keep-going` continues after
individual unavailable pages or failed transfers, prints a failure summary, and
exits 1 if anything failed. Invalid input, authentication/subscription errors,
missing required tools, and local disk/permission failures stop the whole batch.

## Resuming, upgrades, and concurrent runs

Direct MP4 downloads use `filename.mp4.part` and a small `.part.json`
sidecar identifying the source, size, quality, and available HTTP validators.
Signed URLs are not stored in the sidecar. Resuming checks `Content-Range`;
strong ETags or modification dates are sent with `If-Range` when available.
A server that returns the full file instead of a range causes a restart.

If the source changes or an old partial has no matching metadata, downloading
restarts with a warning. This includes partials made by older script versions.
`--restart` explicitly restarts partial downloads. Existing complete files
are skipped, and higher-quality complete copies are kept when a lower ceiling
is requested.

Smaller versions come from ORORO's HLS stream and require ffmpeg; interrupted
HLS downloads restart. If a higher quality is requested later, the old playable
file stays in place until its replacement is complete. Exact known-size downloads
check free space before starting; HLS space requirements remain estimates.

A per-video advisory lock prevents simultaneous ororo processes from writing to
the same file. Small hidden `.ororo.lock` files remain in video directories so
lock ownership cannot race with lock-file deletion.

Progress animation is enabled only on a terminal. Use `--no-progress` to
disable it explicitly. Status messages and failure summaries remain available.

## JSON and automation

`--json` writes exactly one JSON result to stdout; diagnostic messages go
to stderr. It also disables prompts and progress animation.

```sh
ororo search "the martian" --type movies --json
ororo info https://ororo.tv/en/shows/heroes --season 1 --json
ororo -i list.txt --dry-run --keep-going --json
ororo https://ororo.tv/en/movies/the-martian --non-interactive --no-progress
```

Results include `command` and `ok`. Search/info return `items`.
Download/preview results include `items`, a `summary`, and `errors`.
Byte counts are integers, unknown sizes are `null`, and estimates are marked
with `estimated`. Download results describe per-file plans, not measured
network traffic including retries. Signed download URLs and passwords are omitted.

Exit statuses: 0 success (including no search matches), 1 completed with item
failures under `--keep-going`, 2 command/global failure, 130 interrupted.
Help and version output remain plain text.

## Tests

```sh
python3 -m unittest -v
```

Tests use temporary directories and simulated API/HTTP responses; they do not
contact ORORO or read your real credentials.
