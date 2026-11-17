"""Embedding models. An index is only valid with the model that built it.

hash-v1           built in, offline, no download. Hashed TF-IDF: each word is
                  hashed into one of 4096 buckets, weighted by how rare it was
                  in the corpus WHEN THE INDEX WAS BUILT (the IDF table is
                  stored with the index). Matches words, not meaning.
nomic-embed-text  real neural embedding model served by Ollama (768 dims).
                  Matches meaning ("can't sign in" ~ "login fails").

Why this is a deployment quantum: a query must be embedded with the SAME
model (and, for hash-v1, the same IDF table) as the index it searches.
Vectors from different models live in different spaces, so switching model
means re-embedding every document, i.e. a full re-index, and similarity
scores change scale, so the Chat confidence threshold must be re-calibrated.
"""
import hashlib
import math
import os
import re
from collections import Counter

import httpx

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")

_TOKEN = re.compile(r"[a-z0-9]+")
STOPWORDS = set("""a an the is are was were be been am i me my we our you your it its to of in on at
for from by with and or not no do does did how what why when where which who can could should
would will this that these those there here if then than so as into out up down about""".split())


def _stem(w: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    return w


def terms(text: str) -> list[str]:
    return [_stem(w) for w in _TOKEN.findall(text.lower()) if w not in STOPWORDS]


class HashEmbedder:
    name = "hash-v1"
    BUCKETS = 4096

    def __init__(self, idf: dict[str, float] | None = None):
        self.idf = idf or {}

    @staticmethod
    def _bucket(term: str) -> str:
        # md5, not hash(): Python's hash() is randomised per process, so vectors
        # would not match across restarts.
        return str(int(hashlib.md5(term.encode()).hexdigest()[:8], 16) % HashEmbedder.BUCKETS)

    @classmethod
    def fit(cls, texts: list[str]) -> "HashEmbedder":
        """Compute the IDF table from the corpus being indexed."""
        df = Counter()
        for t in texts:
            df.update({cls._bucket(x) for x in terms(t)})
        n = len(texts)
        return cls({b: math.log((1 + n) / (1 + c)) + 1 for b, c in df.items()})

    def embed(self, texts: list[str]) -> list[dict[str, float]]:
        out = []
        default_idf = max(self.idf.values(), default=1.0)   # unseen word = as rare as the rarest
        for t in texts:
            tf = Counter(self._bucket(x) for x in terms(t))
            vec = {b: (1 + math.log(c)) * self.idf.get(b, default_idf) for b, c in tf.items()}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            out.append({b: v / norm for b, v in vec.items()})
        return out

    def state(self) -> dict:
        return {"idf": self.idf}


class OllamaEmbedder:
    name = "nomic-embed-text"

    def __init__(self, _state: dict | None = None, timeout: float = 60.0):
        self.timeout = timeout

    @classmethod
    def fit(cls, texts: list[str]) -> "OllamaEmbedder":
        return cls()

    def embed(self, texts: list[str]) -> list[list[float]]:
        r = httpx.post(f"{OLLAMA_URL}/api/embed", json={"model": self.name, "input": texts},
                       timeout=self.timeout)
        r.raise_for_status()
        out = []
        for v in r.json()["embeddings"]:
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out

    def state(self) -> dict:
        return {}


MODELS = {HashEmbedder.name: HashEmbedder, OllamaEmbedder.name: OllamaEmbedder}


def load(model: str, state: dict) -> HashEmbedder | OllamaEmbedder:
    if model == HashEmbedder.name:
        return HashEmbedder(state.get("idf"))
    if model == OllamaEmbedder.name:
        return OllamaEmbedder(state)
    raise ValueError(f"unknown embedding model {model}")


def similarity(a, b) -> float:
    """Cosine similarity of two unit vectors (sparse dict or dense list)."""
    if isinstance(a, dict):
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(k, 0.0) for k, v in a.items())
    return sum(x * y for x, y in zip(a, b))
