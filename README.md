# tmuxmux

A terminal UI for hopping between tmux sessions scattered across many hosts.

One sidebar lists every host in your config and its live tmux sessions. The
main pane is a real PTY running `ssh <host> -t tmux attach -t <session>`, so
tmux / vim / anything inside the remote session works exactly like a normal
terminal. `Ctrl-]` and `Ctrl-\` tab between sessions; if a session has died
since you last tabbed away, tmuxmux reconnects on the spot.

## Requirements

- Python 3.11+ (uses `tomllib`)
- `ssh` in your `$PATH`, plus agent / keys set up for each host
- `tmux` on each host (and locally if you use `local = true`)
- Optional: `sshpass`, `cloudflared`, etc. — only if your `command = …` entries call them

## Install

```bash
git clone <your-fork-url> tmuxmux
cd tmuxmux
cp hosts.example.toml hosts.toml   # then edit for your machines
./run.sh
```

`run.sh` creates a local venv on first run, installs tmuxmux into it, and
launches. You can also install manually:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/tmuxmux              # or: python -m tmuxmux
```

## Config

`hosts.toml` is a TOML array of `[[hosts]]` entries. tmuxmux loads
`./hosts.toml` first, falling back to `~/.config/tmuxmux/hosts.toml`.

Three kinds of host:

```toml
# 1. Local — no ssh, just `tmux ls` / `tmux attach` on this machine.
[[hosts]]
name = "localhost"
local = true

# 2. Plain ssh — anything ssh can resolve.
[[hosts]]
name = "bots"                   # ssh alias, FQDN, or IP
# username = "alice"            # optional

# 3. Custom command — arbitrary shell invocation up to the ssh destination.
#    Must include -tt for tmux attach to get a remote pty.
[[hosts]]
name = "my-app"
command = "sshpass -p hunter2 ssh -tt -o ProxyCommand='cloudflared access ssh --hostname app.example.com' dev@app.example.com"
```

Custom-command hosts are grouped under a single `Manual` heading in the
sidebar.

## Usage

| Key          | Action                                                      |
|--------------|-------------------------------------------------------------|
| `↑` `↓`      | Preview the session under the cursor (pane follows)         |
| `Enter`      | Commit — focus drops into the pane, type straight into tmux |
| `Ctrl-]`     | Next session (works from anywhere, including inside the pane) |
| `Ctrl-\`     | Previous session                                            |
| `F5`         | Re-list tmux sessions on every host                         |
| `Ctrl-Q`     | Quit                                                        |

Sessions are spawned lazily — a host's SSH connection only opens when you
tab to one of its sessions. Dead sessions auto-reconnect the next time
you focus them.

## How it works

- Sidebar: Textual `Tree`, populated by running `ssh <host> tmux ls` in
  parallel on startup.
- Main pane: a custom `TerminalPane` widget that forks a PTY (`pty.fork`),
  feeds the child's output into a [`pyte`](https://github.com/selectel/pyte)
  screen, and renders it via `render_line`. Keystrokes are translated to
  xterm-compatible byte sequences and written back to the PTY.
- DEC special-graphics charset (`ESC(0`) is enabled so tmux's box-drawing
  borders render as real Unicode glyphs instead of literal `q`/`x`.
- `$TMUX` and `$TMUX_PANE` are stripped in the child env so launching
  tmuxmux from inside tmux doesn't refuse to nest.

## Known caveats

- `hosts.toml` lives in plaintext. If it holds passwords (e.g. for
  `sshpass`), treat the file accordingly — it's in `.gitignore` for that
  reason.
- `Ctrl-\` sends `SIGQUIT` in a normal tty; tmuxmux binds it for
  prev-session before it reaches the embedded terminal. If you need
  SIGQUIT passthrough, rebind it in `tmuxmux/app.py`.
- Attaching to your current tmux session (on localhost) works, but you'll
  be looking at the tmux you're running tmuxmux in, inside that same
  tmux — confusing but harmless.

## License

MIT
