#!/usr/bin/env python3
"""
Start one or more servers, wait for them to be ready, run a command, then clean up.

Usage:
    # Single server
    python scripts/with_server.py --server "npm run dev" --port 5173 -- python automation.py
    python scripts/with_server.py --server "npm start" --port 3000 -- python test.py

    # Multiple servers
    python scripts/with_server.py \
      --server "cd backend && python server.py" --port 3000 \
      --server "cd frontend && npm run dev" --port 5173 \
      -- python test.py

A server only counts as ready once a connection to localhost:PORT is accepted
by a process the helper started. The helper refuses to start if PORT is already
in use, fails if a server exits, and holds every other loopback address for
PORT so no other process can take it while the command runs.
"""

import argparse
import errno
import ipaddress
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

LOOPBACK_HOSTS = ('127.0.0.1', '::1')
ACCEPT_WAIT = 2.0


def _normalize_ip(host):
    ip = ipaddress.ip_address(host.split('%', 1)[0])
    if ip.version == 6 and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def loopback_addresses(port):
    """Every (family, sockaddr) a client resolving 'localhost' may connect to."""
    seen = set()
    addrs = []
    candidates = []
    try:
        candidates.extend(socket.getaddrinfo('localhost', port, type=socket.SOCK_STREAM))
    except socket.gaierror:
        pass
    for host in LOOPBACK_HOSTS:
        try:
            candidates.extend(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except socket.gaierror:
            pass
    for family, _type, _proto, _canon, sockaddr in candidates:
        key = (family, _normalize_ip(sockaddr[0]))
        if key not in seen:
            seen.add(key)
            addrs.append((family, sockaddr))
    return addrs


def _connect(family, sockaddr):
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect(sockaddr)
    except OSError:
        sock.close()
        return None
    return sock


def port_in_use(port):
    for family, sockaddr in loopback_addresses(port):
        sock = _connect(family, sockaddr)
        if sock is not None:
            sock.close()
            return True
    return False


def _parse_proc_addr(text):
    host_hex, port_hex = text.split(':')
    raw = bytes.fromhex(host_hex)
    if sys.byteorder == 'little':
        raw = b''.join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    return _normalize_ip(str(ipaddress.ip_address(raw))), int(port_hex, 16)


def _proc_session_connections(sid):
    """TCP connections of every process in session `sid`, read from /proc."""
    inodes = set()
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open(f'/proc/{entry}/stat') as f:
                stat = f.read()
            fields = stat[stat.rfind(')') + 2:].split()
            if int(fields[3]) != sid:
                continue
            fd_dir = f'/proc/{entry}/fd'
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(f'{fd_dir}/{fd}')
                except OSError:
                    continue
                if target.startswith('socket:['):
                    inodes.add(target[8:-1])
        except (OSError, IndexError, ValueError):
            continue

    connections = set()
    for table in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(table) as f:
                lines = f.readlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10 or parts[9] not in inodes:
                continue
            connections.add(_parse_proc_addr(parts[1]) + _parse_proc_addr(parts[2]))
    return connections


def _parse_lsof_endpoint(text):
    if text.startswith('['):
        host, _, port = text[1:].partition(']:')
    else:
        host, _, port = text.rpartition(':')
    return _normalize_ip(host), int(port)


def _lsof_group_connections(pgid):
    """TCP connections of every process in process group `pgid`, read via lsof."""
    result = subprocess.run(
        ['lsof', '-nP', '-a', '-g', str(pgid), '-iTCP', '-F', 'n'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    connections = set()
    for line in result.stdout.splitlines():
        if not line.startswith('n') or '->' not in line:
            continue
        local, remote = line[1:].split('->', 1)
        try:
            connections.add(_parse_lsof_endpoint(local) + _parse_lsof_endpoint(remote))
        except ValueError:
            continue
    return connections


def owned_connections(process):
    """TCP connections held by the process tree started for `process`.

    The server runs in its own session (and process group) whose id is its pid,
    so other users' processes can never be counted as part of it.
    """
    if os.path.isdir('/proc/self/fd') and os.path.exists('/proc/net/tcp'):
        return _proc_session_connections(process.pid)
    if shutil.which('lsof'):
        return _lsof_group_connections(process.pid)
    raise RuntimeError(
        "Cannot verify which process owns the server port: need /proc or lsof"
    )


def _probe(process, family, sockaddr, port, deadline):
    """Return 'refused', 'owned', or 'foreign' for one loopback address."""
    sock = _connect(family, sockaddr)
    if sock is None:
        return 'refused'
    try:
        client_host, client_port = sock.getsockname()[:2]
        client = (_normalize_ip(client_host), client_port)
        end = min(time.monotonic() + ACCEPT_WAIT, deadline)
        while True:
            for local_ip, local_port, remote_ip, remote_port in owned_connections(process):
                if local_port == port and (remote_ip, remote_port) == client:
                    return 'owned'
            if time.monotonic() >= end:
                return 'foreign'
            time.sleep(0.05)
    finally:
        sock.close()


def _reserve(family, sockaddr):
    """Bind (without listening) so no other process can serve this address.

    Returns the socket, None if the address is unusable on this host, or raises
    OSError if something else already holds it. SO_REUSEADDR is deliberately
    left unset so another socket cannot share the binding.
    """
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return None
    try:
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind(sockaddr)
    except OSError as exc:
        sock.close()
        if exc.errno in (errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT):
            return None
        raise
    return sock


def wait_for_server(process, port, timeout=30):
    """Wait until every loopback address for `port` is served by `process`'s tree
    or reserved by this helper. Returns the reservation sockets to hold open."""
    deadline = time.monotonic() + timeout
    last_problem = "nothing accepted connections"
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"Server for port {port} exited with code {code} before it was ready")

        addrs = loopback_addresses(port)
        states = [(family, sockaddr, _probe(process, family, sockaddr, port, deadline))
                  for family, sockaddr in addrs]
        foreign = [sockaddr[0] for _f, sockaddr, state in states if state == 'foreign']
        if foreign:
            last_problem = f"a process not started by this helper accepted on {', '.join(foreign)}"
        elif any(state == 'owned' for _f, _s, state in states):
            reservations = []
            try:
                for family, sockaddr, state in states:
                    if state == 'refused':
                        sock = _reserve(family, sockaddr)
                        if sock is not None:
                            reservations.append(sock)
                if process.poll() is None:
                    return reservations
            except OSError as exc:
                last_problem = f"could not reserve {sockaddr[0]}:{port} ({exc.strerror})"
            for sock in reservations:
                sock.close()
        time.sleep(0.5)
    raise RuntimeError(
        f"Server failed to start on port {port} within {timeout}s: {last_problem}"
    )


def stop_server(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (AttributeError, ProcessLookupError, PermissionError):
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError, PermissionError):
            process.kill()
        process.wait()


def main():
    parser = argparse.ArgumentParser(description='Run command with one or more servers')
    parser.add_argument('--server', action='append', dest='servers', required=True, help='Server command (can be repeated)')
    parser.add_argument('--port', action='append', dest='ports', type=int, required=True, help='Port for each server (must match --server count)')
    parser.add_argument('--timeout', type=int, default=30, help='Timeout in seconds per server (default: 30)')
    parser.add_argument('command', nargs=argparse.REMAINDER, help='Command to run after server(s) ready')

    args = parser.parse_args()

    # Remove the '--' separator if present
    if args.command and args.command[0] == '--':
        args.command = args.command[1:]

    if not args.command:
        print("Error: No command specified to run")
        sys.exit(1)

    # Parse server configurations
    if len(args.servers) != len(args.ports):
        print("Error: Number of --server and --port arguments must match")
        sys.exit(1)

    servers = []
    for cmd, port in zip(args.servers, args.ports):
        servers.append({'cmd': cmd, 'port': port})

    for server in servers:
        if port_in_use(server['port']):
            print(f"Error: Port {server['port']} is already in use by another process. "
                  f"Stop it or choose a different port.")
            sys.exit(1)

    server_processes = []
    reservations = []

    try:
        # Start all servers
        for i, server in enumerate(servers):
            print(f"Starting server {i+1}/{len(servers)}: {server['cmd']}")

            # Use shell=True to support commands with cd and &&. A new session
            # lets readiness checks and cleanup cover the whole process tree.
            process = subprocess.Popen(
                server['cmd'],
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            server_processes.append(process)

            # Wait for this server to be ready
            print(f"Waiting for server on port {server['port']}...")
            reservations.extend(wait_for_server(process, server['port'], timeout=args.timeout))

            print(f"Server ready on port {server['port']}")

        print(f"\nAll {len(servers)} server(s) ready")

        # Run the command, stopping it if a server dies and frees its port
        print(f"Running: {' '.join(args.command)}\n")
        command = subprocess.Popen(args.command)
        while True:
            try:
                returncode = command.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                exited = [s['port'] for s, p in zip(servers, server_processes) if p.poll() is not None]
                if exited:
                    command.terminate()
                    command.wait()
                    raise RuntimeError(f"Server on port {exited[0]} exited while the command was running")
        sys.exit(returncode)

    finally:
        for sock in reservations:
            sock.close()
        # Clean up all servers
        print(f"\nStopping {len(server_processes)} server(s)...")
        for i, process in enumerate(server_processes):
            stop_server(process)
            print(f"Server {i+1} stopped")
        print("All servers stopped")


if __name__ == '__main__':
    main()
