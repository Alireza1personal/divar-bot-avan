# -*- coding: utf-8 -*-
"""
Dynamic location mapping + safe contextual resolution (Phase 2).

• sync():     GET {API}/locations/mappings (X-Bot-Token) + GET {API}/places/tree  → kept in memory only.
              The local config.* tables are never modified.
• resolve():  raw Divar district text → (city, region, neighborhood) or a `needs_review` result.

Resolution order (first decisive layer wins; ambiguity STOPS the chain — no blind guessing):
  A. dynamic mappings   (city+region+raw) → (city+raw) → (raw alone)
  B. live places tree   (exact / «raw(qualifier)» names inside the city)
  C. strict static config.DISTRICT_TO_PANEL (offline fallback; ambiguous generics are never defaulted)
Duplicate names (e.g. «هفت تیر») are disambiguated ONLY by evidence in the ad text
(qualifier such as «آب و برق», or «منطقه ۹»); no evidence / conflicting evidence → needs_review.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

import config

SYNC_INTERVAL_SEC = 600


def fold_key(v) -> str:
    """Must match the server's fold() (src/lib/geoScope.js): ي→ی, ك→ک, digits→ASCII, ZWNJ/NBSP→space, collapse, lower."""
    if v is None:
        return ""
    s = str(v).replace("ي", "ی").replace("ك", "ک")
    s = s.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    s = re.sub(r"[‌‏‎ ]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


@dataclass
class Loc:
    city: str
    region: str
    neighborhood: str
    nid: str = ""


@dataclass
class Resolution:
    city: str = ""
    region: str = ""
    neighborhood: str = ""
    source: str = ""            # mapping | tree | static | ""
    needs_review: bool = False
    reason: str = ""


def _qualifier(name: str) -> str:
    m = re.search(r"[\(（](.+?)[\)）]", name or "")
    return fold_key(m.group(1)) if m else ""


def _region_mentioned(region: str, text: str) -> bool:
    r = fold_key(region)
    if not r:
        return False
    if re.search(rf"منطقه\s*{re.escape(r)}(?!\d)", text):
        return True
    return len(r) > 2 and re.search(rf"(?<!\S){re.escape(r)}(?!\S)", text) is not None


def _evidence(loc: Loc, text: str) -> bool:
    q = _qualifier(loc.neighborhood)
    if q and q in text:
        return True
    return _region_mentioned(loc.region, text)


def _pick_by_evidence(cands: list[Loc], text: str) -> Optional[Loc]:
    """Qualifier hit («آب و برق») decides first; region mention («منطقه ۹») only if no qualifier hit.
    Exactly one candidate must win, otherwise None (ambiguous)."""
    q = [c for c in cands if _qualifier(c.neighborhood) and _qualifier(c.neighborhood) in text]
    if q:
        return q[0] if len(q) == 1 else None
    r = [c for c in cands if _region_mentioned(c.region, text)]
    return r[0] if len(r) == 1 else None


class LocationResolver:
    def __init__(self, api_url: str = "", token: str = "", mappings_path: str = "/locations/mappings"):
        self.api_url = (api_url or "").rstrip("/")
        self.token = token or ""
        self.mappings_path = mappings_path
        self.mappings: list[dict] = []
        self.tree: list[dict] = []
        self._nb: dict[str, Loc] = {}          # neighborhood id → Loc
        self._etag: dict[str, str] = {}
        self._synced_at = 0.0

    # ── sync ──────────────────────────────────────────────────────────
    def _get(self, path: str, auth: bool):
        headers = {"Accept": "application/json"}
        if auth:
            headers["X-Bot-Token"] = self.token
        if self._etag.get(path):
            headers["If-None-Match"] = self._etag[path]
        r = requests.get(f"{self.api_url}{path}", headers=headers, timeout=15)
        if r.status_code == 304:
            return None
        r.raise_for_status()
        if r.headers.get("ETag"):
            self._etag[path] = r.headers["ETag"]
        return r.json()

    def sync(self) -> bool:
        """Fetch mappings + tree. On any failure keep what we already have (never raises)."""
        if not (self.api_url and self.token):
            return False
        ok = True
        try:
            data = self._get(self.mappings_path, True)
            if data is not None:
                self.mappings = [m for m in (data.get("mappings") or []) if m.get("placeType") == "neighborhood"]
        except Exception as e:
            ok = False
            print(f"[mapping] sync mappings failed: {e}")
        try:
            data = self._get("/places/tree", False)
            if data is not None:
                self.tree = data.get("cities") or []
                self._index_tree()
        except Exception as e:
            ok = False
            print(f"[mapping] sync tree failed: {e}")
        self._synced_at = time.time()
        print(f"[mapping] {len(self.mappings)} mappings, {len(self._nb)} neighborhoods in tree")
        return ok

    def refresh_if_stale(self) -> None:
        if self.api_url and self.token and time.time() - self._synced_at > SYNC_INTERVAL_SEC:
            self.sync()

    def load(self, mappings: list[dict], tree: list[dict]) -> None:
        """Inject data directly (tests / offline)."""
        self.mappings = [m for m in mappings if m.get("placeType") == "neighborhood"]
        self.tree = tree
        self._index_tree()

    def _index_tree(self) -> None:
        nb: dict[str, Loc] = {}
        for c in self.tree:
            for r in c.get("regions", []):
                for n in r.get("neighborhoods", []):
                    nb[str(n["id"])] = Loc(c["name"], r["name"], n["name"], str(n["id"]))
        self._nb = nb

    def knows_raw(self, district: str) -> bool:
        k = fold_key(district)
        return bool(k) and any(m.get("rawKey") == k for m in self.mappings)

    # ── resolution ────────────────────────────────────────────────────
    def resolve(self, district: str, context: str = "") -> Resolution:
        raw = (district or "").strip()
        key = fold_key(raw)
        text = fold_key(f"{raw} {context}")
        if not key:
            return self._review(raw, "empty district")
        city_f = fold_key(config.CITY_NAME)

        for layer in (self._by_mapping, self._by_tree, self._by_static):
            status, loc = layer(key, city_f, text)
            if status == "ok":
                return Resolution(loc.city, loc.region, loc.neighborhood, source=layer.__name__[4:])
            if status == "ambiguous":
                return self._review(raw, f"ambiguous ({layer.__name__[4:]})")
        return self._review(raw, "no match")

    def _review(self, raw: str, reason: str) -> Resolution:
        # raw neighborhood kept untouched; region empty; city = the (known) city being scraped
        return Resolution(city=config.CITY_NAME, region="", neighborhood=raw, needs_review=True, reason=reason)

    # A — dynamic mappings
    def _generic(self, key: str, city_f: str) -> bool:
        return len(self._tree_candidates(key, city_f)) > 1

    def _by_mapping(self, key: str, city_f: str, text: str):
        entries = [m for m in self.mappings if m.get("rawKey") == key and m.get("cityKey", "") in (city_f, "")]
        if not entries:
            return "none", None
        tiers = [
            [m for m in entries if m.get("cityKey") == city_f and m.get("regionKey")],  # city+region+raw
            [m for m in entries if m.get("cityKey") == city_f and not m.get("regionKey")],  # city+raw
            [m for m in entries if not m.get("cityKey") and not m.get("regionKey")],  # raw alone
        ]
        for i, tier in enumerate(tiers):
            if i == 0:
                # region-specific rows only count when the region is evidenced in the ad
                tier_ok = [m for m in tier if m["regionKey"] and (_region_mentioned(m["regionKey"], text))]
                if not tier_ok:
                    # region-dependent mapping with no region evidence: usable only if all rows agree on one place
                    if tier and len({m["placeId"] for m in tier}) == 1 and not any(tiers[1:]):
                        tier_ok = tier
                    else:
                        continue
                tier = tier_ok
            locs = {}
            for m in tier:
                loc = self._nb.get(str(m.get("neighborhoodId") or m["placeId"]))
                if loc:
                    locs[loc.nid] = loc
            if not locs:
                continue
            cands = list(locs.values())
            loc = cands[0] if len(cands) == 1 else _pick_by_evidence(cands, text)
            if loc is None:
                return "ambiguous", None
            # a learned generic mapping («هفت تیر» → X) must not silently override duplicate names
            # → don't decide here; let the live-tree layer pick by the ad's own evidence (or flag it)
            if self._generic(key, city_f) and not _evidence(loc, text):
                return "none", None
            return "ok", loc
        return "none", None

    # B — live tree
    def _tree_candidates(self, key: str, city_f: str) -> list[Loc]:
        exact, prefixed = [], []
        for loc in self._nb.values():
            if fold_key(loc.city) != city_f:
                continue
            n = fold_key(loc.neighborhood)
            if n == key:
                exact.append(loc)
            elif n.startswith(key + "(") or n.startswith(key + " ("):
                prefixed.append(loc)
        return exact + prefixed

    def _by_tree(self, key: str, city_f: str, text: str):
        cands = self._tree_candidates(key, city_f)
        if not cands:
            return "none", None
        exact = [c for c in cands if fold_key(c.neighborhood) == key]
        if len(exact) == 1 and len(cands) == 1:
            return "ok", exact[0]
        loc = _pick_by_evidence(cands, text)
        return ("ok", loc) if loc else ("ambiguous", None)

    # C — strict static fallback (config tables untouched; no default region, no default sub-node)
    def _by_static(self, key: str, city_f: str, text: str):
        if key and city_f != fold_key(config.CITY_NAME):
            return "none", None
        panel = {fold_key(n): n for n in config.PANEL_NEIGHBORHOODS}
        aliases = {fold_key(a): n for a, n in config.DISTRICT_TO_PANEL.items()}
        prefixed = [n for fk, n in panel.items() if fk.startswith(key + "(") or fk.startswith(key + " (")]
        if len(prefixed) > 1:  # generic parent name with several sub-nodes
            names = prefixed
        elif key in aliases:
            names = [aliases[key]]
        else:
            hits = {n for a, n in aliases.items() if a and (a in key or key in a)}
            names = sorted(hits)
        names = [n for n in names if n in config.PANEL_NEIGHBORHOODS]
        if not names:
            return "none", None
        locs = []
        for n in dict.fromkeys(names):
            region = config.NEIGHBORHOOD_TO_REGION.get(n)
            if region:
                locs.append(Loc(config.CITY_NAME, str(region), n))
        if not locs:
            return "ambiguous", None
        if len(names) == 1:
            return ("ok", locs[0]) if len(locs) == 1 else ("ambiguous", None)
        loc = _pick_by_evidence(locs, text)
        return ("ok", loc) if loc else ("ambiguous", None)
