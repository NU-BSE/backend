import json
import math
from collections import Counter
from dataclasses import dataclass

from redis.asyncio import Redis

from app.core.config import Settings


@dataclass(frozen=True)
class VelocitySignals:
    requests_per_minute: int
    requests_per_hour: int
    distinct_ip_count_1h: int
    distinct_asn_count_1h: int
    payload_entropy: float
    flags: list[str]


def _entropy(payloads: list[str]) -> float:
    joined = "".join(payloads)
    if not joined:
        return 0.0
    counts = Counter(joined)
    total = len(joined)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


class StaticAsnLookup:
    async def lookup(self, ip: str) -> str:
        # Replace with a local MaxMind/GeoIP ASN database in production.
        if ip.startswith("127.") or ip == "::1":
            return "local"
        return "unknown"


class RedisRiskProvider:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings

    @staticmethod
    def _key(device_id: str) -> str:
        return f"attestation:velocity:{device_id}"

    async def evaluate(
        self,
        *,
        device_id: str,
        now_ms: int,
        nonce: str,
        ip: str,
        asn: str,
        payload_hash: str,
        first_seen: bool,
    ) -> VelocitySignals:
        key = self._key(device_id)
        event = json.dumps(
            {
                "at_ms": now_ms,
                "nonce": nonce,
                "ip": ip,
                "asn": asn,
                "payload_hash": payload_hash,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        cutoff_24h = now_ms - 24 * 60 * 60 * 1000
        pipe = self.redis.pipeline(transaction=True)
        pipe.zadd(key, {event: now_ms})
        pipe.zremrangebyscore(key, 0, cutoff_24h - 1)
        pipe.expire(key, 25 * 60 * 60)
        pipe.zrangebyscore(key, cutoff_24h, now_ms)
        results = await pipe.execute()
        raw_events = results[-1]
        events = [json.loads(item) for item in raw_events]

        one_minute = now_ms - 60_000
        one_hour = now_ms - 3_600_000
        events_1h = [event for event in events if event["at_ms"] >= one_hour]
        requests_per_minute = sum(event["at_ms"] >= one_minute for event in events_1h)
        requests_per_hour = len(events_1h)
        distinct_ips = len({event["ip"] for event in events_1h})
        distinct_asns = len({event["asn"] for event in events_1h})
        payload_entropy = _entropy([event["payload_hash"] for event in events[-100:]])

        flags: list[str] = []
        if requests_per_minute > self.settings.velocity_burst_requests_per_minute:
            flags.append("BURST")
        if distinct_ips > self.settings.velocity_ip_hopping_distinct_ips_1h:
            flags.append("IP_HOPPING")
        if distinct_asns > self.settings.velocity_asn_hopping_distinct_asns_1h:
            flags.append("ASN_HOPPING")
        if 0 < payload_entropy < self.settings.velocity_low_entropy_bits_per_symbol:
            flags.append("LOW_ENTROPY_PAYLOADS")
        if first_seen:
            flags.append("NEW_DEVICE")

        return VelocitySignals(
            requests_per_minute=requests_per_minute,
            requests_per_hour=requests_per_hour,
            distinct_ip_count_1h=distinct_ips,
            distinct_asn_count_1h=distinct_asns,
            payload_entropy=payload_entropy,
            flags=flags,
        )
