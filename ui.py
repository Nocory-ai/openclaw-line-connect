"""
Moltbot LINE Connect UI Module
Provides rich terminal interface using 'rich' library
"""

from datetime import datetime
from typing import Optional

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.markdown import Markdown
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.theme import Theme
    from rich import print as rprint
except ImportError:
    # Fallback for plain text if rich is missing (will just print)
    Console = None

# Custom theme for brand colors
THEME = Theme({
    "info": "bold cyan",
    "warning": "bold yellow",
    "error": "bold red",
    "success": "bold green",
    "timestamp": "dim white"
})

class UIController:
    """Manages the UI output using Rich"""
    
    def __init__(self):
        if Console:
            self.console = Console(theme=THEME)
        else:
            self.console = None

    def print_logo(self):
        """Display startup logo"""
        if not self.console:
            print("🦞 Moltbot LINE Connect")
            print("======================")
            return

        self.console.print()
        self.console.print("[bold red]🦞 Moltbot LINE Connect[/bold red]")
        self.console.print("==================================================", style="dim")
        self.console.print()

    def create_progress(self):
        """Create a progress context manager"""
        if not self.console:
            return None
        return Progress(
            SpinnerColumn("dots", style="bold red"),
            TextColumn("[progress.description]{task.description}"),
            console=self.console,
            transient=True
        )

    def log(self, message: str, level: str = "INFO"):
        """Log a system message"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        style = "info"
        icon = "ℹ️"
        
        if level == "WARN":
            style = "warning"
            icon = "⚠️"
        elif level == "ERROR":
            style = "error"
            icon = "❌"
        elif level == "SUCCESS":
            style = "success"
            icon = "✅"

        if self.console:
            self.console.print(f"[{timestamp}] {icon} ", end="", style="timestamp")
            self.console.print(message, style=style)
        else:
            print(f"[{timestamp}] [{level}] {message}")

    def log_incoming_message(self, user_name: str, message_text: str):
        """Log an incoming message from LINE"""
        if not self.console:
            print(f"📩 [LINE] {user_name}: {message_text}")
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        self.console.print()
        self.console.print(f"[{timestamp}] 📩 [bold cyan]{user_name}[/bold cyan]: {message_text}")

    def log_reply(self, reply_text: str):
        """Log Moltbot's reply in a panel"""
        if not self.console:
            print(f"✅ Moltbot Replied: {reply_text[:50]}...")
            return

        md = Markdown(reply_text)
        panel = Panel(
            md,
            title="🦞 Moltbot Reply",
            title_align="left",
            border_style="red",
            expand=False
        )
        self.console.print(panel)
        self.console.print()

    def show_status(self, config: dict, tunnel_url: str = None):
        """Show status table"""
        if not self.console:
            print(f"Status: {config.get('status', 'unknown')}")
            print(f"Tunnel: {tunnel_url or 'N/A'}")
            return

        table = Table(show_header=False, box=None)
        table.add_column("Key", style="bold white")
        table.add_column("Value", style="cyan")

        status = config.get('status', 'unknown')
        status_icon = "🟢" if status == 'connected' else "🔴"
        
        table.add_row("Status", f"{status_icon} {status.upper()}")
        table.add_row("Gateway ID", config.get('gateway_id', 'N/A'))
        table.add_row("Tunnel URL", tunnel_url or "Disconnected")
        table.add_row("Connected At", config.get('connected_at', 'N/A'))
        table.add_row("Reconnects", str(config.get('reconnect_count', 0)))

        panel = Panel(
            table,
            title="Service Status",
            border_style="blue"
        )
        self.console.print(panel)
