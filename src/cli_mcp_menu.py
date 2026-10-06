"""McpMenu - MCP server interactive management menu."""
import logging
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.markup import escape

logger = logging.getLogger("hermes.cli.mcp_menu")
console = Console()


class McpMenu:
    """MCP server management menu (interactive)."""

    def __init__(self, agent):
        self.agent = agent
        from src.mcp.client import get_client_manager
        self.mgr = get_client_manager()

    def run(self):
        """Top-level menu loop."""
        while True:
            console.print()
            console.print(Panel(
                "[1] List all servers\n"
                "[2] Manage servers (enable/disable/delete)\n"
                "[0] Back",
                title="[MCP] Server Management",
                border_style="cyan",
                padding=(0, 1),
            ))
            choice = Prompt.ask("Select", default="0", choices=["0", "1", "2"])
            if choice == "0":
                return
            elif choice == "1":
                self._show_list()
            elif choice == "2":
                self._manage()

    def _show_list(self):
        """Show all MCP server statuses."""
        servers = self.mgr.list_servers()
        console.print()
        if not servers:
            console.print(Panel(
                "No MCP servers configured.\n\n"
                "To add:\n"
                "  1. Add mcp_<name>.json under mcp_servers/ (one file per server)\n"
                '  2. Ask Agent: "add a memory server to mcp_servers/"',
                title="[MCP] Servers",
                border_style="dim",
            ))
            return

        table = Table(show_header=True, border_style="dim", title="[MCP] Server List")
        table.add_column("#", style="dim", width=3)
        table.add_column("Name", style="cyan", width=18)
        table.add_column("Type", width=16)
        table.add_column("Status", width=12)
        table.add_column("Tools", justify="right", width=6)

        for i, state in enumerate(servers, 1):
            cfg = state.config
            if not cfg.enabled:
                status = "[dim]disabled[/dim]"
            elif state.connected:
                status = "[green]connected[/green]"
            elif state.error:
                status = "[red]failed[/red]"
            else:
                status = "[yellow]connecting[/yellow]"

            if cfg.transport == "stdio":
                type_str = f"stdio ({cfg.command})"
            else:
                type_str = cfg.transport

            tool_count = str(state.tool_count) if state.connected else "-"
            table.add_row(str(i), escape(cfg.name), escape(type_str), status, tool_count)

        console.print(table)
        connected = sum(1 for s in servers if s.connected)
        total_tools = sum(s.tool_count for s in servers if s.connected)
        console.print(
            f"   [dim]{len(servers)} servers - "
            f"[green]{connected}[/green] connected - "
            f"[green]{total_tools}[/green] MCP tools loaded[/dim]\n"
        )

    def _manage(self):
        """Sub-menu: multi-select server -> choose action."""
        servers = self.mgr.list_servers()
        if not servers:
            console.print("\n  [dim]No MCP servers. Add mcp_<name>.json under mcp_servers/ first.[/dim]\n")
            return

        console.print("\nCurrent servers:")
        for i, state in enumerate(servers, 1):
            cfg = state.config
            if state.connected:
                st = "[green]OK[/green]"
            elif not cfg.enabled:
                st = "[dim]OFF[/dim]"
            elif state.error:
                st = "[red]ERR[/red]"
            else:
                st = "[yellow]...[/yellow]"
            n = state.tool_count if state.connected else 0
            console.print(f"  [{i}] {st} {escape(cfg.name):20s} [dim]({cfg.transport}, {n} tools)[/dim]")

        choice = Prompt.ask(
            "\nSelect servers (comma-separated, e.g. 1,3; Enter to cancel)",
            default="",
        )
        if not choice.strip():
            console.print("  [dim]Cancelled[/dim]\n")
            return

        indices = self._parse_multi_choice(choice, len(servers))
        if not indices:
            console.print("  [red]Invalid input[/red]\n")
            return

        names = [servers[i - 1].config.name for i in indices]
        console.print(f"\n  Selected: [cyan]{', '.join(escape(n) for n in names)}[/cyan]")

        console.print("\n  [1] Enable (connect)")
        console.print("  [2] Disable (disconnect)")
        console.print("  [3] Delete (disconnect + remove from config)")
        action = Prompt.ask("Action", default="0", choices=["0", "1", "2", "3"])
        if action == "0":
            console.print("  [dim]Cancelled[/dim]\n")
            return

        results = []
        for name in names:
            if action == "1":
                ok, msg = self.mgr.set_enabled(name, True)
            elif action == "2":
                ok, msg = self.mgr.set_enabled(name, False)
            elif action == "3":
                ok, msg = self.mgr.remove_server(name)
            else:
                continue
            results.append((name, ok, msg))

        console.print()
        for name, ok, msg in results:
            icon = "[green]OK[/green]" if ok else "[red]FAIL[/red]"
            console.print(f"  {icon} {escape(msg)}")

        try:
            self.agent.rebind_tools()
            console.print("\n  [dim]Tools refreshed[/dim]")
        except Exception as e:
            logger.warning(f"Tool refresh failed: {e}")
            console.print(f"\n  [yellow]Refresh failed: {escape(str(e))}[/yellow]")
        console.print()

    @staticmethod
    def _parse_multi_choice(input_str: str, max_val: int) -> list[int]:
        """Parse multi-select input '1,3,5' -> [1, 3, 5]."""
        result = []
        for part in input_str.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                idx = int(part)
                if 1 <= idx <= max_val and idx not in result:
                    result.append(idx)
            except ValueError:
                continue
        return sorted(result)