#!/usr/bin/env python3
"""
Moltbot LINE Connect - One-click LINE integration for Moltbot
Supports auto-reconnection and error notifications

Usage:
  python moltbot_line.py connect     Start service and display QR Code
  python moltbot_line.py daemon      Background service mode (auto-reconnect)
  python moltbot_line.py status      Check connection status
  python moltbot_line.py disconnect  Disconnect from LINE
"""

import argparse
import asyncio
import json
import sys
import os
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

try:
    import aiohttp
except ImportError:
    print("Please install aiohttp: pip install aiohttp")
    sys.exit(1)

from tunnel_manager import TunnelManager
from local_proxy import LocalProxy
from ui import UIController
from qr_generator import print_qr_code

# SaaS API endpoint
SAAS_API = "https://moltbot-line.nocory.ai/api/client"

# Configuration directory
CONFIG_DIR = Path.home() / ".moltbot" / "line"
LOG_FILE = CONFIG_DIR / "service.log"


class LineConnectService:
    """LINE Connect Service with reconnection and error notification support"""
    
    def __init__(self, daemon_mode: bool = False):
        self.daemon_mode = daemon_mode
        self.tunnel: Optional[TunnelManager] = None
        self.proxy: Optional[LocalProxy] = None
        self.current_token: Optional[str] = None
        self._running = False
        self.ui = UIController()
        
    def _log(self, message: str, level: str = "INFO", console_log: bool = True):
        """Log message to file and console"""
        # Use Rich UI for console output
        if not self.daemon_mode and console_log:
            self.ui.log(message, level)
        
        # Write to log file
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_line = f"[{timestamp}] [{level}] {message}"
            
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, 'a') as f:
                f.write(log_line + "\n")
        except Exception:
            pass
    
    def _on_tunnel_connect(self, url: str):
        """Callback when tunnel connects successfully"""
        self._log(f"Connected to cloud")
        config = load_config() or {}
        config.update({
            "tunnel_url": url,
            "local_port": 8787,
            "connected_at": datetime.now().isoformat(),
            "status": "connected"
        })
        save_config(config)
    
    def _on_tunnel_disconnect(self, reason: str):
        """Callback when tunnel disconnects"""
        self._log(f"⚠️ Tunnel disconnected: {reason}", "WARN")
        update_config({"status": "disconnected", "last_disconnect": datetime.now().isoformat()})
        
        # Notify bound users about offline status
        asyncio.create_task(self._notify_users_offline())
    
    def _on_tunnel_reconnect(self, url: str, attempt: int):
        """Callback when tunnel reconnects successfully"""
        config = load_config() or {}
        gateway_id = config.get('gateway_id', 'unknown')
        self._log(f"🔄 Reconnected (attempt {attempt}): {gateway_id}")
        config.update({
            "tunnel_url": url,
            "connected_at": datetime.now().isoformat(),
            "status": "connected",
            "reconnect_count": attempt
        })
        save_config(config)
        
        # Update tunnel URL with SaaS (keep same gateway_id)
        asyncio.create_task(self._update_gateway_tunnel(url))
    
    def _on_tunnel_error(self, error: Exception):
        """Callback when tunnel encounters an error"""
        self._log(f"❌ Tunnel error: {error}", "ERROR")
        update_config({"last_error": str(error), "error_at": datetime.now().isoformat()})
    
    async def _notify_users_offline(self):
        """Notify bound users that service is offline"""
        try:
            config = load_config()
            if not config or not config.get('tunnel_url'):
                return
            
            # Call SaaS API to notify users
            async with aiohttp.ClientSession() as session:
                encoded_url = config['tunnel_url'].replace('/', '%2F').replace(':', '%3A')
                async with session.post(
                    f"{SAAS_API}/notify-offline",
                    json={"tunnel_domain": config['tunnel_url']},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        self._log("Notified users about offline status")
        except Exception as e:
            self._log(f"Failed to notify users: {e}", "WARN")
    
    async def _re_register_tunnel(self, new_url: str):
        """Re-register new tunnel URL with SaaS"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{SAAS_API}/update-tunnel",
                    json={"old_tunnel": load_config().get('tunnel_url'), "new_tunnel": new_url},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        self._log(f"Updated tunnel URL")
        except Exception as e:
            self._log(f"Failed to update tunnel URL: {e}", "WARN")
    
    async def _update_gateway_tunnel(self, new_url: str):
        """Update gateway's tunnel URL (keeps same gateway_id)"""
        config = load_config()
        if not config:
            return
        
        gateway_id = config.get('gateway_id')
        if not gateway_id:
            return
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{SAAS_API}/gateway/update",
                    json={"gateway_id": gateway_id, "tunnel_url": new_url},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        self._log(f"Updated gateway: {gateway_id}", console_log=False)
                        update_config({"tunnel_url": new_url})
        except Exception as e:
            self._log(f"Failed to update gateway: {e}", "WARN")
    
    async def _check_existing_binding(self, tunnel_url: str) -> bool:
        """Check if there are existing users bound to previous tunnel"""
        config = load_config()
        if not config or not config.get('tunnel_url'):
            return False
        
        old_tunnel = config.get('tunnel_url')
        gateway_id = config.get('gateway_id')
        
        # If we have a gateway_id, just update the tunnel URL
        if gateway_id:
            if old_tunnel != tunnel_url:
                await self._update_gateway_tunnel(tunnel_url)
            return True
        
        # No gateway_id - check if there are bound users and get new gateway
        try:
            encoded_url = old_tunnel.replace('/', '%2F').replace(':', '%3A')
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{SAAS_API}/status/{encoded_url}",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        users = data.get('users', [])
                        if len(users) > 0:
                            # Has existing users, register to get gateway_id
                            print(f"\n📋 Found {len(users)} existing binding(s)")
                            print("     Upgrading to new gateway system...")
                            
                            # Register to get gateway_id
                            reg_data = await self._register_tunnel(tunnel_url)
                            if reg_data and reg_data.get('gateway_id'):
                                update_config({
                                    'gateway_id': reg_data['gateway_id'],
                                    'tunnel_url': tunnel_url
                                })
                                print(f"     ✅ Gateway: {reg_data['gateway_id']}")
                            
                            # Update users to new tunnel URL
                            await self._re_register_tunnel(tunnel_url)
                            return True
        except Exception as e:
            self._log(f"Failed to check existing binding: {e}", "WARN")
        
        return False
    
    async def start(self):
        """Start the service"""
        self._running = True
        
        self.ui.print_logo()
        
        try:
            # Use Rich Progress
            progress = self.ui.create_progress()
            
            # Use a dummy context manager if progress is None (no rich)
            cm = progress if progress else open(os.devnull)
            
            with cm:
                task_id = None
                if progress:
                    task_id = progress.add_task("Starting services...", total=3)
                
                # 1. Start local proxy server
                if progress: progress.update(task_id, description="[1/3] Starting local proxy server...")
                else: print("[1/3] Starting local proxy server...")
                
                self.proxy = LocalProxy(port=8787, ui_controller=self.ui)
                await self.proxy.start()
                
                if progress: 
                    progress.advance(task_id)
                    time.sleep(0.2) # Visual effect
                else: print("     ✅ Proxy running on http://127.0.0.1:8787")
                
                # 2. Connect to Moltbot Cloud
                if progress: progress.update(task_id, description="[2/3] Connecting to Moltbot Cloud...")
                else: print("[2/3] Connecting to Moltbot Cloud...")
                
                self.tunnel = TunnelManager(
                    on_connect=self._on_tunnel_connect,
                    on_disconnect=self._on_tunnel_disconnect,
                    on_reconnect=self._on_tunnel_reconnect,
                    on_error=self._on_tunnel_error,
                    max_retries=10,
                    retry_delay=5.0
                )
                tunnel_url = await self.tunnel.start(local_port=8787)
                
                if progress: 
                    progress.advance(task_id)
                    time.sleep(0.2)
                else: print("     ✅ Connected")
                
                # 3. Check for existing binding
                if progress: progress.update(task_id, description="[3/3] Checking binding status...")
                else: print("[3/3] Checking binding status...")
                
                has_existing = await self._check_existing_binding(tunnel_url)
                
                if progress: 
                    progress.update(task_id, description="✨ Service Ready!", completed=3)
                    time.sleep(0.5)
                
                if has_existing:
                    if not progress: print("     ✅ Reconnected to existing binding")
                else:
                    if not progress: print("[4/4] Ready!")
                # New binding mode - get token and show QR
                data = await self._register_tunnel(tunnel_url)
                if not data:
                    return
                
                self.current_token = data['token']
                gateway_id = data.get('gateway_id', 'unknown')
                
                # Save gateway_id to config
                update_config({'gateway_id': gateway_id})
                
                print(f"     ✅ Gateway: {gateway_id}")
                
                # Display QR Code
                print("\n[4/4] Scan QR Code to complete binding:")
                print()
                print_qr_code(data['deep_link'])
                print()
                print(f"📱 Or open this link:")
                print(f"   {data['deep_link']}")
                print()
                print(f"⏰ Token expires in: {data['expires_in']} seconds")
            
            print()
            print("-" * 50)
            
            if self.daemon_mode:
                print("🔄 Daemon mode (auto-reconnect enabled)")
            else:
                print("🔄 Service running... (Press Ctrl+C to stop)")
            
            print("-" * 50)
            
            # Start health check loop
            health_task = asyncio.create_task(self._health_check_loop())
            
            # Keep running
            while self._running:
                await asyncio.sleep(1)
                
        except KeyboardInterrupt:
            print("\n\nShutting down...")
        except Exception as e:
            print(f"\n❌ Error: {e}")
            self._log(f"Service error: {e}", "ERROR")
        finally:
            await self.stop()
    
    async def _register_tunnel(self, tunnel_url: str) -> Optional[dict]:
        """Register tunnel with SaaS"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{SAAS_API}/register",
                    json={"tunnel_domain": tunnel_url},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        print(f"     ❌ Registration failed: {error}")
                        return None
                    return await resp.json()
        except Exception as e:
            print(f"     ❌ Registration failed: {e}")
            return None
    
    async def _health_check_loop(self):
        """Periodic health check"""
        # Wait for service to fully start
        await asyncio.sleep(5)
        
        while self._running:
            try:
                
                if self.tunnel and self.tunnel.is_running():
                    stats = self.tunnel.get_stats()
                    uptime_min = int(stats.get('uptime_seconds', 0) / 60)
                    reconnects = stats.get('reconnect_count', 0)
                    
                    if reconnects > 0:
                        self._log(f"💓 Health check: running {uptime_min} min, {reconnects} reconnects")
                    
                    # Send heartbeat to SaaS to keep gateway online
                    asyncio.create_task(self._update_gateway_tunnel(stats.get('url', load_config().get('tunnel_url'))))

                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log(f"Health check error: {e}", "WARN")
            
            # Wait for next check
            await asyncio.sleep(60)
    
    async def stop(self):
        """Stop the service"""
        self._running = False
        
        if self.tunnel:
            await self.tunnel.stop()
        if self.proxy:
            await self.proxy.stop()
        
        update_config({"status": "stopped", "stopped_at": datetime.now().isoformat()})
        print("👋 All services stopped")


async def connect():
    """Start binding flow"""
    service = LineConnectService(daemon_mode=False)
    await service.start()


async def daemon():
    """Background service mode"""
    service = LineConnectService(daemon_mode=True)
    await service.start()


async def status():
    """Check connection status"""
    print("\n🦞 Moltbot LINE Connect - Status")
    print("=" * 50)
    
    config = load_config()
    if not config:
        print("\n❌ Not configured. Run: python moltbot_line.py connect")
        return
    
    gateway_id = config.get('gateway_id')
    
    print(f"\n🌐 Gateway: {gateway_id or 'N/A'}")
    print(f"📊 Status: {config.get('status', 'unknown')}")
    
    if config.get('connected_at'):
        print(f"⏰ Connected at: {config.get('connected_at')}")
    
    if config.get('reconnect_count'):
        print(f"🔄 Reconnect count: {config.get('reconnect_count')}")
    
    if config.get('last_error'):
        print(f"❌ Last error: {config.get('last_error')}")
    
    # Check gateway status from SaaS
    if gateway_id:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{SAAS_API}/gateway/{gateway_id}",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        is_online = data.get('is_online', False)
                        print(f"\n☁️ Cloud status: {'🟢 Online' if is_online else '🔴 Offline'}")
                    else:
                        print(f"\n⚠️ Cloud status: Unknown")
        except Exception as e:
            print(f"\n❌ Cloud status: Unreachable")
    
    # Check local tunnel reachability
    tunnel_url = config.get('tunnel_url')
    if tunnel_url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{tunnel_url}/health",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Support both old (gateway status) and new (simple status) formats
                        if data.get('gateway'):
                            gw_status = data.get('gateway')
                        elif data.get('status') == 'ok':
                            gw_status = 'connected'
                        else:
                            gw_status = 'unknown'
                            
                        print(f"🔗 Moltbot Gateway: {gw_status}")
                    else:
                        print(f"⚠️ Moltbot Gateway: Error ({resp.status})")
        except Exception as e:
            # Try localhost fallback if tunnel is unreachable (e.g. DNS issues)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"http://127.0.0.1:8787/health",
                        timeout=aiohttp.ClientTimeout(total=2)
                    ) as resp:
                         if resp.status == 200:
                             print(f"🔗 Moltbot Gateway: connected (via localhost)")
                         else:
                             print(f"❌ Moltbot Gateway: Unreachable")
            except Exception:
                print(f"❌ Moltbot Gateway: Unreachable")
    
    # Check bound users using gateway_id
    if gateway_id:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{SAAS_API}/gateway/{gateway_id}/users",
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        users = data.get('users', [])
                        print(f"\n👤 Bound users: {len(users)}")
                        if len(users) == 0:
                            print("   (Scan QR code to bind your LINE account)")
                        for user in users:
                            status_icon = "✅" if user.get('is_active') else "❌"
                            name = user.get('display_name', 'Unknown')
                            print(f"   {status_icon} {name}")
                    else:
                        print(f"\n👤 Bound users: Unknown")
        except Exception as e:
            print(f"\n⚠️ Cannot retrieve user status")
    
    # Show recent logs (filter out tunnel URLs)
    if LOG_FILE.exists():
        print(f"\n📋 Recent logs:")
        try:
            lines = LOG_FILE.read_text().strip().split('\n')[-5:]
            for line in lines:
                # Hide cloudflare URLs in logs
                if 'trycloudflare.com' in line:
                    continue
                print(f"   {line}")
        except Exception:
            pass


async def disconnect():
    """Disconnect from service"""
    print("\n🦞 Moltbot LINE Connect - Disconnect")
    print("=" * 50)
    
    config = load_config()
    if config:
        config_file = CONFIG_DIR / "config.json"
        if config_file.exists():
            config_file.unlink()
        print("\n✅ Local configuration cleared")
    
    print("\n📝 Note: LINE binding still exists on server side")
    print("   User needs to block the bot in LINE to fully unbind")


async def logs():
    """View service logs"""
    print("\n🦞 Moltbot LINE Connect - Service Logs")
    print("=" * 50)
    
    if not LOG_FILE.exists():
        print("\n❌ No logs available")
        return
    
    try:
        content = LOG_FILE.read_text().strip()
        lines = content.split('\n')[-50:]  # Last 50 lines
        print()
        for line in lines:
            print(line)
    except Exception as e:
        print(f"\n❌ Failed to read logs: {e}")


def save_config(config: dict):
    """Save configuration to file"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_file = CONFIG_DIR / "config.json"
    config_file.write_text(json.dumps(config, indent=2))


def update_config(updates: dict):
    """Update existing configuration"""
    config = load_config() or {}
    config.update(updates)
    save_config(config)


def load_config() -> Optional[dict]:
    """Load configuration from file"""
    config_file = CONFIG_DIR / "config.json"
    if config_file.exists():
        try:
            return json.loads(config_file.read_text())
        except Exception:
            return None
    return None


def main():
    parser = argparse.ArgumentParser(
        description='Moltbot LINE Connect - Connect LINE to your Moltbot (with auto-reconnect)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python moltbot_line.py connect     Start service and display QR Code
  python moltbot_line.py daemon      Background service mode (auto-reconnect)
  python moltbot_line.py status      Check connection status
  python moltbot_line.py logs        View service logs
  python moltbot_line.py disconnect  Disconnect from service
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    subparsers.add_parser('connect', help='Connect to LINE (scan QR code)')
    subparsers.add_parser('daemon', help='Background service mode (auto-reconnect)')
    subparsers.add_parser('status', help='Check connection status')
    subparsers.add_parser('logs', help='View service logs')
    subparsers.add_parser('disconnect', help='Disconnect from service')
    
    args = parser.parse_args()
    
    if args.command == 'connect':
        asyncio.run(connect())
    elif args.command == 'daemon':
        asyncio.run(daemon())
    elif args.command == 'status':
        asyncio.run(status())
    elif args.command == 'logs':
        asyncio.run(logs())
    elif args.command == 'disconnect':
        asyncio.run(disconnect())
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
