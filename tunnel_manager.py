"""
Cloudflare Tunnel Manager (with auto-reconnect)
Automatically downloads cloudflared and creates Quick Tunnel with auto-reconnection support
"""

import asyncio
import platform
import re
import shutil
import stat
import tarfile
import io
from pathlib import Path
from typing import Optional, Callable
from datetime import datetime

try:
    import aiohttp
except ImportError:
    raise ImportError("Please install aiohttp: pip install aiohttp")


# Cloudflared download URLs
CLOUDFLARED_URLS = {
    ('Darwin', 'x86_64'): 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz',
    ('Darwin', 'arm64'): 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz',
    ('Linux', 'x86_64'): 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64',
    ('Linux', 'aarch64'): 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64',
    ('Windows', 'AMD64'): 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe',
}


class TunnelManager:
    """Manage Cloudflare Tunnel with auto-reconnection"""
    
    def __init__(
        self,
        on_connect: Optional[Callable[[str], None]] = None,
        on_disconnect: Optional[Callable[[str], None]] = None,
        on_reconnect: Optional[Callable[[str, int], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        max_retries: int = 10,
        retry_delay: float = 5.0
    ):
        """
        Initialize Tunnel Manager
        
        Args:
            on_connect: Callback when connection succeeds (tunnel_url)
            on_disconnect: Callback when disconnected (reason)
            on_reconnect: Callback when reconnection succeeds (tunnel_url, attempt)
            on_error: Callback when error occurs (exception)
            max_retries: Maximum retry attempts
            retry_delay: Delay between retries in seconds
        """
        self.process = None
        self.tunnel_url = None
        self._cloudflared_path = None
        self._cache_dir = Path.home() / '.moltbot' / 'bin'
        self._local_port = 8787
        self._should_run = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._retry_count = 0
        
        # Configuration
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        
        # Callbacks
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_reconnect = on_reconnect
        self.on_error = on_error
        
        # Statistics
        self.stats = {
            'connected_at': None,
            'disconnected_count': 0,
            'reconnect_count': 0,
            'last_error': None,
            'uptime_seconds': 0
        }
    
    async def _ensure_cloudflared(self):
        """Ensure cloudflared is installed"""
        # Check if already installed on system
        system_path = shutil.which('cloudflared')
        if system_path:
            self._cloudflared_path = system_path
            return
        
        # Check local cache
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        
        system = platform.system()
        machine = platform.machine()
        
        # Handle Mac M1/M2 architecture names
        if machine == 'arm64' and system == 'Darwin':
            machine = 'arm64'
        elif machine == 'x86_64':
            machine = 'x86_64'
        
        binary_name = 'cloudflared.exe' if system == 'Windows' else 'cloudflared'
        binary_path = self._cache_dir / binary_name
        
        if binary_path.exists():
            self._cloudflared_path = str(binary_path)
            return
        
        # Download
        print("     Downloading connector...")
        url = CLOUDFLARED_URLS.get((system, machine))
        if not url:
            raise RuntimeError(f"Unsupported platform: {system} {machine}")
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Download failed: HTTP {resp.status}")
                content = await resp.read()
        
        if url.endswith('.tgz'):
            # macOS requires extraction
            with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as tar:
                for member in tar.getmembers():
                    if member.name.endswith('cloudflared'):
                        member.name = binary_name
                        tar.extract(member, self._cache_dir)
                        break
        else:
            binary_path.write_bytes(content)
        
        # Set executable permission (Unix)
        if system != 'Windows':
            binary_path.chmod(binary_path.stat().st_mode | stat.S_IEXEC)
        
        self._cloudflared_path = str(binary_path)
        print(f"     Installed connector")
    
    async def _start_tunnel(self) -> str:
        """Start tunnel once and return URL"""
        await self._ensure_cloudflared()
        
        cmd = [
            self._cloudflared_path,
            'tunnel',
            '--url', f'http://127.0.0.1:{self._local_port}',
            '--no-autoupdate'
        ]
        
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Wait for tunnel URL to appear
        url_pattern = re.compile(r'(https://[a-z0-9-]+\.trycloudflare\.com)')
        timeout = 30
        start_time = asyncio.get_event_loop().time()
        
        while True:
            if asyncio.get_event_loop().time() - start_time > timeout:
                if self.process:
                    self.process.terminate()
                raise RuntimeError("Timeout waiting for tunnel URL")
            
            try:
                line = await asyncio.wait_for(
                    self.process.stderr.readline(),
                    timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            
            if not line:
                stdout, stderr = await self.process.communicate()
                raise RuntimeError(f"Tunnel startup failed: {stderr.decode()}")
            
            line_text = line.decode()
            match = url_pattern.search(line_text)
            if match:
                self.tunnel_url = match.group(1)
                self.stats['connected_at'] = datetime.now()
                return self.tunnel_url
    
    async def _monitor_tunnel(self):
        """Monitor tunnel status with auto-reconnect"""
        while self._should_run:
            try:
                # Check if process is still alive
                if self.process and self.process.returncode is not None:
                    # Process has exited, need to reconnect
                    self.stats['disconnected_count'] += 1
                    reason = f"Process exited (code: {self.process.returncode})"
                    
                    if self.on_disconnect:
                        self.on_disconnect(reason)
                    
                    # Attempt reconnection
                    await self._reconnect()
                
                # Check every 5 seconds
                await asyncio.sleep(5)
                
                # Update uptime
                if self.stats['connected_at']:
                    delta = datetime.now() - self.stats['connected_at']
                    self.stats['uptime_seconds'] = delta.total_seconds()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.stats['last_error'] = str(e)
                if self.on_error:
                    self.on_error(e)
    
    async def _reconnect(self):
        """Reconnection logic"""
        self._retry_count = 0
        
        while self._should_run and self._retry_count < self.max_retries:
            self._retry_count += 1
            print(f"\n🔄 Reconnecting... (attempt {self._retry_count}/{self.max_retries})")
            
            try:
                # Clean up old process
                await self._stop_process()
                
                # Wait before retry
                await asyncio.sleep(self.retry_delay)
                
                # Start new tunnel
                new_url = await self._start_tunnel()
                
                self.stats['reconnect_count'] += 1
                print(f"✅ Reconnected! New URL: {new_url}")
                
                if self.on_reconnect:
                    self.on_reconnect(new_url, self._retry_count)
                
                self._retry_count = 0
                return
                
            except Exception as e:
                self.stats['last_error'] = str(e)
                print(f"❌ Reconnection failed: {e}")
                
                if self.on_error:
                    self.on_error(e)
        
        if self._retry_count >= self.max_retries:
            print(f"\n💀 Max retries reached ({self.max_retries}), stopping reconnection")
            self._should_run = False
    
    async def _stop_process(self):
        """Stop tunnel process"""
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
            self.process = None
    
    async def start(self, local_port: int = 8787) -> str:
        """
        Start tunnel and begin monitoring
        
        Args:
            local_port: Local proxy port
        
        Returns:
            Public tunnel URL
        """
        self._local_port = local_port
        self._should_run = True
        
        # Start tunnel
        url = await self._start_tunnel()
        
        if self.on_connect:
            self.on_connect(url)
        
        # Start monitoring task
        self._monitor_task = asyncio.create_task(self._monitor_tunnel())
        
        return url
    
    async def stop(self):
        """Stop tunnel and monitoring"""
        self._should_run = False
        
        # Stop monitoring
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        
        # Stop process
        await self._stop_process()
        self.tunnel_url = None
    
    def is_running(self) -> bool:
        """Check if tunnel is running"""
        return self.process is not None and self.process.returncode is None
    
    def get_stats(self) -> dict:
        """Get running statistics"""
        return {
            **self.stats,
            'is_running': self.is_running(),
            'tunnel_url': self.tunnel_url,
            'retry_count': self._retry_count
        }
