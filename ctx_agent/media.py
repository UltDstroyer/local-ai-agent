"""Owner-invoked media tools. Not accessible through the autonomous agent tool API.

OBS recording is controlled interactively by the user. Narration uses an
explicitly enrolled local voice reference via optional F5-TTS. Neither
component receives arbitrary desktop file access from an AI request.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import uuid
from pathlib import Path

VOICE_LIMIT = 25 * 1024 * 1024


def profile_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return base / "ctx-agent" / "media"


def enroll(wav: Path, transcript: str) -> Path:
    if wav.is_symlink() or not wav.is_file() or wav.suffix.lower() != ".wav":
        raise ValueError("Use a real, local .wav reference recording")
    if not transcript.strip() or len(transcript) > 500:
        raise ValueError("Provide an accurate transcript, at most 500 characters")
    if wav.stat().st_size > VOICE_LIMIT:
        raise ValueError("Reference file exceeds 25 MiB")
    folder = profile_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    dest = folder / "voice.wav"
    with wav.open("rb") as src, dest.open("wb") as output:
        shutil.copyfileobj(src, output)
    os.chmod(dest, 0o600)
    transcript_path = folder / "transcript.txt"
    transcript_path.write_text(transcript, encoding="utf-8")
    os.chmod(transcript_path, 0o600)
    return dest


def synthesize(text: str, output: Path, maximum: bool = False):
    if not text.strip() or len(text) > 10000:
        raise ValueError("Narration must contain 1 to 10000 characters")
    if output.suffix.lower() != ".wav" or output.exists():
        raise ValueError("Choose a new .wav output file")
    voice = profile_dir() / "voice.wav"
    reference = profile_dir() / "transcript.txt"
    if not voice.is_file() or not reference.is_file():
        raise ValueError("Enroll your own voice reference first")
    binary = shutil.which("f5-tts_infer-cli")
    if not binary:
        raise RuntimeError("F5-TTS is not installed; see docs/MEDIA.md")
    output.parent.mkdir(parents=True, exist_ok=True)
    args = [binary, "--model", "F5TTS_v1_Base",
            "--ref_audio", str(voice), "--ref_text", reference.read_text(),
            "--gen_text", text, "--output_dir", str(output.parent),
            "--output_file", output.name]
    subprocess.run(args, check=True, timeout=900,
                   start_new_session=True,
                   # Slightly lower CPU scheduling priority unless maximum mode.
                   preexec_fn=None if maximum else lambda: os.nice(10))
    if not output.is_file():
        raise RuntimeError("Voice synthesizer did not produce the output WAV")
    return output


def mux(video: Path, audio: Path, output: Path):
    if not video.is_file() or not audio.is_file():
        raise ValueError("Video and audio must exist")
    if output.exists() or output.suffix.lower() != ".mp4":
        raise ValueError("Choose a new MP4 output path")
    if output.resolve() in (video.resolve(), audio.resolve()):
        raise ValueError("Output must be a different path")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-i", str(video), "-i", str(audio),
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-shortest",
                    "-n", str(output)], check=True, timeout=1800)
    return output


def _read_exact(sock: socket.socket, n: int) -> bytes:
    data = bytearray()
    while len(data) < n:
        block = sock.recv(n - len(data))
        if not block:
            raise ConnectionError("OBS closed the WebSocket connection")
        data.extend(block)
    return bytes(data)


def _recv_frame(sock: socket.socket) -> dict:
    while True:
        first, second = _read_exact(sock, 2)
        length = second & 127
        if length == 126:
            length = struct.unpack("!H", _read_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _read_exact(sock, 8))[0]
        if length > 1024 * 1024:
            raise ValueError("OBS response too large")
        payload = _read_exact(sock, length)
        opcode = first & 15
        if opcode == 8:
            raise ConnectionError("OBS closed WebSocket")
        if opcode == 9:
            _send_frame(sock, payload, opcode=10)
            continue
        if opcode != 1:
            continue
        return json.loads(payload)


def _send_frame(sock: socket.socket, payload: bytes, opcode: int = 1):
    import secrets
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n <= 65535:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", n))
    mask = secrets.token_bytes(4)
    sock.sendall(bytes(header) + mask +
                 bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _obs_send(sock: socket.socket, value: dict):
    _send_frame(sock, json.dumps(value).encode("utf-8"))


def obs_record(action: str):
    if action not in ("start", "stop"):
        raise ValueError("Unknown OBS action")
    password = os.environ.get("CTX_AGENT_OBS_PASSWORD")
    if not password:
        raise ValueError("Set CTX_AGENT_OBS_PASSWORD for your local OBS WebSocket")
    host, port = "127.0.0.1", 4455
    with socket.create_connection((host, port), timeout=6) as sock:
        sock.settimeout(8)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        sock.sendall(req.encode("ascii"))
        # Read only headers; preserve any OBS frame bytes received with them
        # by consuming byte-by-byte until CRLFCRLF.
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            header.extend(_read_exact(sock, 1))
            if len(header) > 10000:
                raise ConnectionError("Invalid OBS HTTP handshake")
        if not header.startswith(b"HTTP/1.1 101"):
            raise ConnectionError("OBS WebSocket handshake rejected")
        hello = _recv_frame(sock)
        if hello.get("op") != 0:
            raise ConnectionError("Unexpected OBS protocol hello")
        identify = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = hello.get("d", {}).get("authentication")
        if auth:
            secret = base64.b64encode(
                hashlib.sha256((password + auth["salt"]).encode()).digest()
            ).decode()
            identify["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()
            ).decode()
        _obs_send(sock, {"op": 1, "d": identify})
        identified = _recv_frame(sock)
        if identified.get("op") != 2:
            raise PermissionError("OBS authentication failed")
        request_id = uuid.uuid4().hex
        _obs_send(sock, {"op": 6, "d": {
            "requestType": "StartRecord" if action == "start" else "StopRecord",
            "requestId": request_id}})
        for _ in range(10):
            response = _recv_frame(sock)
            if response.get("op") == 7 and response.get("d", {}).get("requestId") == request_id:
                data = response["d"]
                status = data.get("requestStatus", {})
                if not status.get("result"):
                    raise RuntimeError("OBS refused recording request: " +
                                       str(status.get("comment", "")))
                return data.get("responseData", {})
        raise RuntimeError("No OBS recording response")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ctx-agent-media")
    sub = parser.add_subparsers(dest="action", required=True)
    e = sub.add_parser("enroll")
    e.add_argument("reference", type=Path)
    e.add_argument("--transcript", required=True)
    v = sub.add_parser("narrate")
    v.add_argument("text")
    v.add_argument("output", type=Path)
    v.add_argument("--maximum", action="store_true")
    m = sub.add_parser("mux")
    m.add_argument("video", type=Path)
    m.add_argument("audio", type=Path)
    m.add_argument("output", type=Path)
    o = sub.add_parser("record")
    o.add_argument("action", choices=("start", "stop"))
    args = parser.parse_args(argv)
    try:
        if args.action == "enroll":
            result = enroll(args.reference, args.transcript)
        elif args.action == "narrate":
            result = synthesize(args.text, args.output, args.maximum)
        elif args.action == "mux":
            result = mux(args.video, args.audio, args.output)
        else:
            result = obs_record(args.action)
        print(result)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print("ctx-agent-media:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
