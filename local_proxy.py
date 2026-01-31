"""
Local Proxy Server
Receives requests from Cloudflare Tunnel and forwards to Moltbot Gateway via CLI
"""

import json
import asyncio
import uuid
import subprocess
import shlex
import shutil
import os
from pathlib import Path
from typing import Optional, Dict, Any

try:
    from aiohttp import web
except ImportError:
    raise ImportError("Please install aiohttp: pip install aiohttp")


class MoltbotCLIClient:
    """
    Client that wraps 'clawdbot gateway call' CLI command
    """
    
    def __init__(self, ui_controller=None):
        self.ui = ui_controller
        self._cached_executable = None

    def _detect_executable(self) -> Optional[str]:
        """Detect available installed binary"""
        if self._cached_executable:
            return self._cached_executable
            
        candidates = ['clawdbot', 'openclaw', 'moltbot']
        for binary in candidates:
            if shutil.which(binary):
                if self.ui:
                    self.ui.log(f"Detected local agent: {binary}", "DEBUG")
                self._cached_executable = binary
                return binary
        
        # Also check common locations if not in PATH
        home = Path.home()
        paths = [
            # User Go bin
            home / "go/bin/clawdbot",
            home / "go/bin/openclaw",
            home / "go/bin/moltbot",
            # Standard locations
            Path("/usr/local/bin/clawdbot"),
            Path("/usr/local/bin/openclaw"),
            Path("/usr/local/bin/moltbot"),
            # Apple Silicon Homebrew
            Path("/opt/homebrew/bin/clawdbot"),
            Path("/opt/homebrew/bin/openclaw"),
            Path("/opt/homebrew/bin/moltbot"),
        ]
        
        for path in paths:
            if path.exists() and os.access(path, os.X_OK):
                self._cached_executable = str(path)
                return str(path)
                
        return None

    async def run_agent(self, message: str, user_id: str, metadata: dict = None) -> dict:
        """Run agent with a message via CLI"""
        
        # Use line: prefix for session ID to isolate LINE users
        session_id = f"line:{user_id}"
        run_id = str(uuid.uuid4())
        
        # Prepend context to message since we can't pass 'context' param
        display_name = (metadata or {}).get('displayName', 'User')
        contextualized_message = f"[LINE User: {display_name}] {message}"
        
        params = {
            "message": contextualized_message,
            "sessionId": session_id,
            "idempotencyKey": run_id
        }
        
        params_json = json.dumps(params)
        
        # Detect executable
        executable = self._detect_executable()
        if not executable:
            return {'ok': False, 'error': 'Moltbot/OpenClaw executable not found via CLI'}

        # Construct command
        cmd = [
            executable, 'gateway', 'call', 'agent',
            '--params', params_json,
            '--expect-final',
            '--timeout', '120000',
            '--json'
        ]
        
        if self.ui:
            self.ui.log(f"Invoking Moltbot... (Session: {session_id})")
        else:
            print(f"   🚀 Invoking Moltbot: {message[:30]}... (Session: {session_id})")
            
        try:
            # Run CLI command asynchronously
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await proc.communicate()
            
            if proc.returncode != 0:
                error_msg = stderr.decode().strip()
                print(f"   ❌ CLI Error: {error_msg}")
                return {'ok': False, 'error': error_msg}
            
            output = stdout.decode().strip()
            
            try:
                # Parse JSON output from CLI
                result = json.loads(output)
                return result
            except json.JSONDecodeError:
                print(f"   ❌ Invalid JSON from CLI: {output[:100]}")
                return {'ok': False, 'error': 'Invalid CLI output'}
                
        except Exception as e:
            print(f"   ❌ Execution Error: {e}")
            return {'ok': False, 'error': str(e)}


class LocalProxy:
    """
    Local Proxy Server
    """
    
    def __init__(self, port: int = 8787, ui_controller=None):
        self.port = port
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self.ui = ui_controller
        self.client = MoltbotCLIClient(ui_controller=ui_controller)
        self._message_count = 0
        self.ui = ui_controller
        self._setup_routes()
    
    def _setup_routes(self):
        """Set up routes"""
        self.app.router.add_get('/health', self.health_check)
        self.app.router.add_post('/line/webhook', self.handle_line_webhook)
        self.app.router.add_get('/', self.index)
        self.app.router.add_get('/status', self.status)
    
    async def index(self, request: web.Request) -> web.Response:
        """Index page"""
        return web.json_response({
            'service': 'moltbot-line-proxy',
            'status': 'running',
            'mode': 'cli-wrapper'
        })
    
    async def health_check(self, request: web.Request) -> web.Response:
        """Health check endpoint"""
        return web.json_response({
            'status': 'ok',
            'gateway': 'connected',
            'message_count': self._message_count
        })
    
    async def status(self, request: web.Request) -> web.Response:
        """Detailed status"""
        return web.json_response({
            'service': 'moltbot-line-proxy',
            'version': '1.0.0',
            'mode': 'cli-wrapper',
            'stats': {
                'message_count': self._message_count
            }
        })
    
    async def handle_line_webhook(self, request: web.Request) -> web.Response:
        """Handle LINE events"""
        try:
            data = await request.json()
            
            event = data.get('event', {})
            message = event.get('message', {})
            user_id = data.get('userId')
            
            self._message_count += 1
            
            user_initials = user_id[:8] if user_id else "Unknown"
            display_name = data.get('displayName', user_initials)
            
            if self.ui:
                self.ui.log_incoming_message(display_name, message.get('text', 'Media Content...'))
            else:
                print(f"📩 [LINE] Received: {message.get('type', 'unknown')} from {display_name}...")
            
            # Forward to Moltbot via CLI
            if message.get('type') == 'text':
                text = message.get('text', '')
                
                # Run agent
                response = await self.client.run_agent(
                    message=text,
                    user_id=user_id,
                    metadata={
                        'event': event,
                        'replyToken': data.get('replyToken'),
                        'displayName': display_name
                    }
                )
                
                # Check for response from Moltbot
                auth_resp = response.get('result', {})
                payloads = auth_resp.get('payloads', [])
                
                reply_text = ""
                if payloads:
                    for p in payloads:
                        if p.get('text'):
                            reply_text += p.get('text') + "\n"
                
                if reply_text:
                    if self.ui:
                        self.ui.log_reply(reply_text.strip())
                    else:
                        print(f"   ✅ Moltbot Replied: {reply_text.strip()[:50]}...")

                    return web.json_response({
                        'success': True,
                        'response': {
                            'text': reply_text.strip(),
                            'raw': response
                        }
                    })
                else:
                     if not self.ui:
                         print(f"   ⚠️ No text response from Moltbot")
                     return web.json_response({'success': True, 'response': None})
                
            else:
                print(f"   ⚠️ Non-text message: {message.get('type')}")
                return web.json_response({
                    'success': True,
                    'reason': 'Non-text message not yet supported'
                })
                
        except json.JSONDecodeError:
            return web.json_response({'error': 'Invalid JSON'}, status=400)
        except Exception as e:
            print(f"   ❌ Error: {e}")
            return web.json_response({'error': str(e)}, status=500)
    
    async def start(self):
        """Start the proxy server"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        
        # Use reuse_address to avoid 'Address already in use' errors
        site = web.TCPSite(self.runner, '127.0.0.1', self.port, reuse_address=True, reuse_port=True)
        await site.start()
    
    async def stop(self):
        """Stop the proxy server"""
        if self.runner:
            await self.runner.cleanup()


async def main():
    proxy = LocalProxy(port=8787)
    await proxy.start()
    print(f"Proxy running on http://127.0.0.1:8787 (CLI Mode)")
    
    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        await proxy.stop()


if __name__ == '__main__':
    asyncio.run(main())
