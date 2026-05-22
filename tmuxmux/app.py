"""Main Textual app for tmuxmux."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import ContentSwitcher, Footer, Header, Static, Tree
from textual.widgets.tree import TreeNode

from . import ssh
from ._log import install_excepthook, log
from .config import Config, Host
from .terminal import TerminalPane


@dataclass(frozen=True)
class SessionKey:
    host_name: str
    session: str

    @property
    def pane_id(self) -> str:
        # ContentSwitcher ids must be valid Textual identifiers. Replace anything
        # awkward.
        safe = f"pane-{self.host_name}-{self.session}"
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in safe)


class TmuxmuxApp(App):
    CSS = """
    Screen { layout: horizontal; }
    #sidebar { width: 34; border-right: solid $primary; }
    #sidebar.-collapsed { display: none; }
    #main { width: 1fr; }
    Tree { padding: 0 1; }
    #placeholder { padding: 2; color: $text-muted; content-align: center middle; }
    """

    BINDINGS = [
        Binding("ctrl+right_square_bracket", "next_session", "Next", priority=True),
        Binding("ctrl+backslash", "prev_session", "Prev", priority=True),
        Binding("f2", "toggle_sidebar", "Sidebar", priority=True),
        Binding("f5", "refresh", "Refresh"),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        install_excepthook()
        log("=== tmuxmux start, {} host(s) ===", len(config.hosts))
        self.config = config
        self._order: list[SessionKey] = []
        self._hosts_by_name: dict[str, Host] = {h.name: h for h in config.hosts}
        self._host_nodes: dict[str, TreeNode] = {}  # plain hosts only
        self._manual_node: TreeNode | None = None
        self._tree: Tree | None = None
        self._switcher: ContentSwitcher | None = None
        self._current: SessionKey | None = None

    # ---------------------------------------------------------------- compose

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            tree: Tree = Tree("Hosts", id="sidebar")
            tree.show_root = False
            tree.guide_depth = 3
            self._tree = tree
            yield tree
            self._switcher = ContentSwitcher(id="main", initial="placeholder")
            with self._switcher:
                yield Static(
                    "No session selected.\nPick one in the sidebar, or press F5 to refresh.",
                    id="placeholder",
                )
        yield Footer()

    def on_mount(self) -> None:
        self.title = "tmuxmux"
        assert self._tree is not None
        plain_hosts = [h for h in self.config.hosts if not h.command]
        manual_hosts = [h for h in self.config.hosts if h.command]
        for host in plain_hosts:
            node = self._tree.root.add(f"{host.name}  (…)", data=host, expand=True)
            self._host_nodes[host.name] = node
        if manual_hosts:
            # Manual hosts get a single shared parent and render flat beneath
            # it — one leaf per session, labeled with the host name (or
            # "host: session" if a host has multiple sessions).
            self._manual_node = self._tree.root.add(
                "Manual", data=None, expand=True
            )
            for host in manual_hosts:
                self._manual_node.add_leaf(
                    f"{host.name}  [dim](…)[/dim]", data=host
                )
        self._tree.focus()
        self.run_worker(self._refresh_all(), exclusive=False)

    # ------------------------------------------------------------ session list

    async def _refresh_all(self) -> None:
        """List tmux sessions on every host in parallel and populate the tree."""
        tasks = [
            asyncio.create_task(self._refresh_one(host))
            for host in self.config.hosts
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Park cursor only after all hosts have reported in so we land on the
        # first session of the first configured host, not whichever came back
        # fastest.
        self.call_after_refresh(self._park_cursor_on_first_leaf)

    async def _refresh_one(self, host: Host) -> None:
        result = await ssh.list_sessions(host)
        if host.command:
            self._apply_manual_result(host, result)
        else:
            self._apply_plain_result(host, result)
        self._rebuild_order()

    def _apply_plain_result(self, host: Host, result: ssh.SessionList) -> None:
        node = self._host_nodes.get(host.name)
        if node is None:
            return
        node.remove_children()
        if result.error:
            node.set_label(f"{host.name}  [red]! {result.error}[/red]")
        elif not result.sessions:
            node.set_label(f"{host.name}  [dim](no sessions)[/dim]")
        else:
            node.set_label(f"{host.name}  [dim]({len(result.sessions)})[/dim]")
            for s in result.sessions:
                node.add_leaf(s, data=SessionKey(host.name, s))

    def _apply_manual_result(self, host: Host, result: ssh.SessionList) -> None:
        """Flat layout under `Manual`. We preserve config order by keeping the
        original placeholder leaf and updating it in place for the common
        (0 or 1 session) case; multi-session rewrites siblings, which rarely
        happens for the apps this grouping targets."""
        if self._manual_node is None:
            return

        def is_ours(child) -> bool:
            d = child.data
            return (
                (isinstance(d, Host) and d.name == host.name)
                or (isinstance(d, SessionKey) and d.host_name == host.name)
            )

        ours = [c for c in self._manual_node.children if is_ours(c)]

        if result.error:
            label, data = f"{host.name}  [red]! {result.error}[/red]", host
            self._update_in_place(ours, label, data)
        elif not result.sessions:
            # Box probably restarted and tmux isn't running. Make the leaf
            # clickable: attach_command for a manual host uses
            # `tmux new-session -A` so this attaches if the session exists
            # or creates a fresh `<host.name>` session in `~/<host.name>`.
            label = f"{host.name}  [dim](start)[/dim]"
            self._update_in_place(ours, label, SessionKey(host.name, host.name))
        elif len(result.sessions) == 1:
            s = result.sessions[0]
            self._update_in_place(ours, host.name, SessionKey(host.name, s))
        else:
            # Multi-session: replace with one leaf per session.
            for c in ours:
                c.remove()
            for s in result.sessions:
                self._manual_node.add_leaf(
                    f"{host.name}: {s}", data=SessionKey(host.name, s)
                )

    def _update_in_place(self, existing, label, data) -> None:
        if not existing:
            if self._manual_node is not None:
                self._manual_node.add_leaf(label, data=data)
            return
        primary, *extras = existing
        primary.set_label(label)
        primary.data = data
        for c in extras:
            c.remove()

    def _park_cursor_on_first_leaf(self) -> None:
        """Once we have any sessions, drop the tree cursor on the first leaf so
        arrow keys / Enter just work."""
        if self._tree is None or not self._order:
            return
        cur = self._tree.cursor_node
        if cur is not None and isinstance(cur.data, SessionKey):
            return
        first = self._order[0]
        leaf = self._find_leaf(first)
        if leaf is not None:
            line = getattr(leaf, "line", -1)
            if line >= 0:
                self._tree.move_cursor_to_line(line)

    def _rebuild_order(self) -> None:
        assert self._tree is not None
        order: list[SessionKey] = []
        for node in self._iter_session_leaves(self._tree.root):
            order.append(node.data)
        self._order = order

    def _iter_session_leaves(self, node: TreeNode):
        for child in node.children:
            if isinstance(child.data, SessionKey):
                yield child
            else:
                yield from self._iter_session_leaves(child)

    def _find_leaf(self, key: SessionKey) -> TreeNode | None:
        if self._tree is None:
            return None
        for leaf in self._iter_session_leaves(self._tree.root):
            if leaf.data == key:
                return leaf
        return None

    # -------------------------------------------------------------- navigation

    async def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        # Enter / click: commit the current session and drop focus into the
        # pane so the user can start typing immediately.
        data = event.node.data
        log("node_selected: data={!r} label={!r}", data, str(event.node.label))
        if isinstance(data, Host) and data.command:
            # Manual host leaf clicked before its `tmux ls` resolved (the
            # `(…)` loading state still has `data=host`). Treat it as a
            # request for the canonical `<host>/<host>` session — the manual
            # attach_command uses `new-session -A`, so this attaches if one
            # already exists and otherwise creates a fresh session in
            # `~/<host>`. Without this the click is a no-op during startup.
            data = SessionKey(data.name, data.name)
        if isinstance(data, SessionKey):
            await self._ensure_mounted(data)
            pane = self._switcher.get_child_by_id(data.pane_id)
            if isinstance(pane, TerminalPane):
                pane.focus()

    async def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        # Arrow-key movement / cursor moves: preview the session in the main
        # pane but keep focus on the tree so subsequent arrows keep navigating.
        data = event.node.data
        if isinstance(data, SessionKey) and data != self._current:
            await self._ensure_mounted(data)

    async def action_next_session(self) -> None:
        self._cycle(+1)

    async def action_prev_session(self) -> None:
        self._cycle(-1)

    def action_refresh(self) -> None:
        self.run_worker(self._refresh_all(), exclusive=False)

    def action_toggle_sidebar(self) -> None:
        if self._tree is None:
            return
        self._tree.toggle_class("-collapsed")
        # Move focus sensibly: hand it to the current pane when hiding (so the
        # user can start typing right away), back to the tree when showing.
        if self._tree.has_class("-collapsed"):
            if self._current is not None and self._switcher is not None:
                try:
                    pane = self._switcher.get_child_by_id(self._current.pane_id)
                except NoMatches:
                    return
                if isinstance(pane, TerminalPane):
                    pane.focus()
        else:
            self._tree.focus()

    def _cycle(self, delta: int) -> None:
        """Move the tree cursor — NodeHighlighted will do the activation."""
        if not self._order or self._tree is None:
            return
        if self._current is None:
            target = self._order[0 if delta > 0 else -1]
        else:
            try:
                i = self._order.index(self._current)
            except ValueError:
                target = self._order[0]
            else:
                target = self._order[(i + delta) % len(self._order)]
        self._move_tree_cursor_to(target)
        # ctrl+] / ctrl+\ are explicit commits — hand focus to the pane so the
        # user can type straight into the remote session.
        try:
            pane = self._switcher.get_child_by_id(target.pane_id)
        except NoMatches:
            pane = None
        if isinstance(pane, TerminalPane):
            pane.focus()

    def _move_tree_cursor_to(self, key: SessionKey) -> None:
        if self._tree is None:
            return
        leaf = self._find_leaf(key)
        if leaf is None:
            return
        line = getattr(leaf, "line", -1)
        if line >= 0:
            self._tree.move_cursor_to_line(line)

    # ---------------------------------------------------------- pane management

    async def _ensure_mounted(self, key: SessionKey) -> None:
        """Make the pane for `key` the current one, mounting it if needed."""
        assert self._switcher is not None
        log("ensure_mounted: key={!r} pane_id={!r}", key, key.pane_id)
        try:
            pane = self._switcher.get_child_by_id(key.pane_id)
            log("  pane exists, reusing")
        except NoMatches:
            host = self._hosts_by_name[key.host_name]
            log("  mounting new pane for host={!r}", host.name)

            def factory(h: Host = host, s: str = key.session) -> list[str]:
                argv = ssh.attach_command(h, s)
                log("  factory built argv: {!r}", argv)
                return argv

            pane = TerminalPane(factory, id=key.pane_id)
            await self._switcher.mount(pane)
        self._switcher.current = key.pane_id
        if isinstance(pane, TerminalPane):
            log("  ensure_alive(); alive_before={}", pane.is_alive)
            pane.ensure_alive()
        self._current = key

    # ------------------------------------------------------------------ events

    def on_terminal_pane_exited(self, event: TerminalPane.Exited) -> None:
        # If the user is looking at the pane that just died, try to respawn
        # right away — matches "if a session dies and I tab to it, immediately
        # try to reconnect" when they're already tabbed in.
        if self._switcher and self._switcher.current == event.pane.id:
            event.pane.ensure_alive()
