"""Tramage réseau compatible avec les canaux jPOS utilisés par le plugin (ASCIIChannel, NACChannel...)."""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass


@dataclass
class ChannelSpec:
    kind: str = "ASCIIChannel"      # ASCIIChannel | NACChannel | NCCChannel | RawChannel
    header: bytes = b""             # en-tête statique (ex. TPDU pour NAC)
    length_digits: int = 4          # ASCIIChannel : nombre de chiffres de longueur

    @classmethod
    def from_config(cls, kind: str, header: str = "", header_hex: bool | None = None,
                    length_digits: int = 4) -> "ChannelSpec":
        if header_hex is None:
            header_hex = kind in ("NACChannel", "NCCChannel")
        hb = bytes.fromhex(header) if header_hex and header else header.encode()
        return cls(kind, hb, length_digits)

    def frame(self, body: bytes) -> bytes:
        data = self.header + body
        n = len(data)
        if self.kind == "ASCIIChannel":
            return str(n).zfill(self.length_digits).encode() + data
        if self.kind == "NACChannel":
            return n.to_bytes(2, "big") + data
        if self.kind == "NCCChannel":
            return bytes.fromhex(str(n).zfill(4)) + data
        if self.kind == "RawChannel":
            return n.to_bytes(4, "big") + data
        raise ValueError(f"Canal non supporté : {self.kind}")

    @property
    def length_size(self) -> int:
        return {"ASCIIChannel": self.length_digits, "NACChannel": 2, "NCCChannel": 2,
                "RawChannel": 4}[self.kind]

    def parse_length(self, raw: bytes) -> int:
        if self.kind == "ASCIIChannel":
            return int(raw.decode())
        if self.kind == "NCCChannel":
            return int(raw.hex())
        return int.from_bytes(raw, "big")

    def strip_header(self, data: bytes) -> tuple[bytes, bytes]:
        h = len(self.header)
        return data[:h], data[h:]

    # ---- asyncio -------------------------------------------------------------
    async def read(self, reader: asyncio.StreamReader) -> bytes:
        raw = await reader.readexactly(self.length_size)
        n = self.parse_length(raw)
        data = await reader.readexactly(n)
        return self.strip_header(data)[1]

    # ---- synchrone (runner natif) --------------------------------------------
    def read_sync(self, sock: socket.socket) -> bytes:
        def recv_exact(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("Connexion fermée par l'hôte")
                buf += chunk
            return buf
        n = self.parse_length(recv_exact(self.length_size))
        return self.strip_header(recv_exact(n))[1]
