"""Operator-owned ring pressure peer; leaves the old public peer unchanged."""

import hashlib
import struct

from network_peer import WirePeer, request, response


class PressurePeer(WirePeer):
    def serve(self):
        try:
            while not self.stopping.is_set():
                try:
                    self.connection, _ = self.listener.accept()
                    break
                except TimeoutError:
                    continue
            if self.connection is None:
                return
            self.connected = True
            self.event("connected")
            with self.connection:
                while not self.stopping.is_set():
                    length = struct.unpack("!I", self.exact(4))[0]
                    if not 16 <= length <= 1514:
                        raise ValueError("Invalid Ethernet length")
                    frame = self.exact(length)
                    if frame[12:14] != b"\x88\xb5":
                        continue
                    seq = int.from_bytes(frame[14:16], "big")
                    valid = frame == request(seq, length)
                    row = {
                        "sequence": seq,
                        "length": length,
                        "valid": valid,
                        "sha256": hashlib.sha256(frame).hexdigest(),
                    }
                    self.frames.append(row)
                    self.event("request", **row)
                    if not valid:
                        self.errors.append("Payload mismatch")
                        continue
                    replies = (
                        [response(request(n, 1514)) for n in range(2000, 2128)]
                        if seq == 2000
                        else [response(frame)]
                    )
                    self.connection.sendall(
                        b"".join(struct.pack("!I", len(r)) + r for r in replies)
                    )
                    self.event("response", sequence=seq, count=len(replies))
        except EOFError:
            self.event("disconnected")
        except (OSError, ValueError, struct.error) as error:
            if not self.stopping.is_set():
                self.errors.append(str(error))
                self.event("error", error=str(error))
