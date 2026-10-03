from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Dict, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


@dataclass
class KalshiSigner:
    api_key_id: str
    private_key: rsa.RSAPrivateKey

    @staticmethod
    def from_pem_file(api_key_id: str, pem_path: str) -> "KalshiSigner":
        with open(pem_path, "rb") as f:
            key_data = f.read()
        private_key = serialization.load_pem_private_key(key_data, password=None)
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise TypeError("Private key is not an RSA private key")
        return KalshiSigner(api_key_id=api_key_id, private_key=private_key)

    def sign_headers(self, method: str, path: str, timestamp_ms: Optional[int] = None) -> Dict[str, str]:
        """Create Kalshi auth headers for the given request.

        IMPORTANT: The signature payload is:
            str(timestamp_ms) + METHOD + PATH
        where PATH is the request path *without* query parameters.
        """
        if timestamp_ms is None:
            timestamp_ms = int(time.time() * 1000)

        payload = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
        sig = self.private_key.sign(
            payload,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(sig).decode("ascii")
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        }
