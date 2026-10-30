"""Caches LLM : exact (hash) et sémantique (similarité locale, sans API d'embeddings)."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

_TOKEN = re.compile(r"[a-zàâçéèêëîïôûùüÿñæœ0-9]+", re.I)
DIM = 1024


def embed(text: str) -> list[float]:
    """Vecteur creux par hachage de mots et bigrammes (déterministe, local, gratuit)."""
    words = [w.lower() for w in _TOKEN.findall(text)]
    feats = Counter(words) + Counter(f"{a}_{b}" for a, b in zip(words, words[1:]))
    vec = [0.0] * DIM
    for f, c in feats.items():
        h = int(hashlib.md5(f.encode()).hexdigest(), 16)
        vec[h % DIM] += (1 if (h >> 12) & 1 else -1) * (1 + math.log(c))
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class LlmCache:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, task TEXT, query TEXT,
                           vec TEXT, response TEXT, model TEXT, created REAL, hits INTEGER DEFAULT 0)""")
        self.db.commit()

    @staticmethod
    def key(task: str, payload: str, context_version: str) -> str:
        return hashlib.sha256(f"{task}|{context_version}|{payload}".encode()).hexdigest()

    def get_exact(self, key: str) -> dict | None:
        row = self.db.execute("SELECT response FROM cache WHERE key=?", (key,)).fetchone()
        if row:
            self.db.execute("UPDATE cache SET hits=hits+1 WHERE key=?", (key,))
            self.db.commit()
            return json.loads(row[0])
        return None

    def get_semantic(self, task: str, query: str, threshold: float) -> tuple[dict, float] | None:
        q = embed(query)
        best, best_s = None, 0.0
        for key, vec, resp in self.db.execute("SELECT key, vec, response FROM cache WHERE task=?", (task,)):
            s = cosine(q, json.loads(vec))
            if s > best_s:
                best, best_s = (key, resp), s
        if best and best_s >= threshold:
            self.db.execute("UPDATE cache SET hits=hits+1 WHERE key=?", (best[0],))
            self.db.commit()
            return json.loads(best[1]), best_s
        return None

    def put(self, key: str, task: str, query: str, response: dict, model: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?,?,?,0)",
                        (key, task, query, json.dumps(embed(query)), json.dumps(response), model, time.time()))
        self.db.commit()

    def stats(self) -> dict:
        n, hits = self.db.execute("SELECT COUNT(*), COALESCE(SUM(hits),0) FROM cache").fetchone()
        return {"entries": n, "hits": hits}
