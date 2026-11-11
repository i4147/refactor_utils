from functools import lru_cache
import json
from pathlib import Path
import re
from urllib.request import urlretrieve

__version__ = "0.0.1-mock"
_PRETOKENIZE = re.compile(
    r"""'s|'t|'re|'ve|'m|'ll|'d| ?[^\W\d_]+| ?\d+| ?[^\s\w]+|\s+(?!\S)|\s+"""
)


@lru_cache()
def _bytes_to_unicode():
    bs = (
        list(range(ord("!"), ord("~") + 1))
        + list(range(ord("¡"), ord("¬") + 1))
        + list(range(ord("®"), ord("ÿ") + 1))
    )
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, (chr(c) for c in cs)))


_BYTE_ENCODER = _bytes_to_unicode()
_BYTE_DECODER = {v: k for k, v in _BYTE_ENCODER.items()}


class Encoding:
    __slots__ = ("ids", "tokens", "attention_mask")

    def __init__(self, ids, tokens, attention_mask):
        self.ids = ids
        self.tokens = tokens
        self.attention_mask = attention_mask

    def __repr__(self):
        return f"Encoding(num_tokens={len(self.ids)})"


def _get_ranked_pairs(word, merges):
    pairs = list(zip(word, word[1:]))
    return [(merges[p], p) for p in pairs if p in merges]


class Tokenizer:
    def __init__(self, vocab=None, merges=None, unk_token="<unk>"):
        self.vocab = dict(vocab) if vocab else {}
        self.merges = dict(merges) if merges else {}
        self.unk_token = unk_token
        self.added_tokens = {}
        self.id_to_token_map = {v: k for k, v in self.vocab.items()}
        self._bpe_cache = {}

    def _bpe(self, token):
        if token in self._bpe_cache:
            return self._bpe_cache[token]
        word = list(token)
        while len(word) > 1:
            ranked = _get_ranked_pairs(word, self.merges)
            if not ranked:
                break
            _, (first, second) = min(ranked)
            new_word, i = [], 0
            while i < len(word):
                if i < len(word) - 1 and word[i] == first and word[i + 1] == second:
                    new_word.append(first + second)
                    i += 2
                else:
                    new_word.append(word[i])
                    i += 1
            word = new_word
        self._bpe_cache[token] = word
        return word

    def encode(self, text, add_special_tokens=True):
        specials = [t for t in self.added_tokens if t]
        if specials:
            split_re = re.compile("(" + "|".join(re.escape(t) for t in specials) + ")")
            segments = split_re.split(text)
        else:
            segments = [text]
        ids, tokens = [], []
        for seg in segments:
            if not seg:
                continue
            if seg in self.added_tokens:
                tokens.append(seg)
                ids.append(self.added_tokens[seg])
                continue
            for piece in _PRETOKENIZE.findall(seg):
                mapped = "".join(_BYTE_ENCODER[b] for b in piece.encode("utf-8"))
                for bt in self._bpe(mapped):
                    tokens.append(bt)
                    ids.append(self.vocab.get(bt, self.vocab.get(self.unk_token, 0)))
        return Encoding(ids=ids, tokens=tokens, attention_mask=[1] * len(ids))

    def decode(self, ids, skip_special_tokens=True):
        specials = set(self.added_tokens)
        chunks = []
        buf = bytearray()

        def flush():
            if buf:
                chunks.append(bytes(buf).decode("utf-8", errors="replace"))
                buf.clear()

        for i in ids:
            tok = self.id_to_token_map.get(i, self.unk_token)
            if tok in specials:
                flush()
                if not skip_special_tokens:
                    chunks.append(tok)
                continue
            for ch in tok:
                if ch in _BYTE_DECODER:
                    buf.append(_BYTE_DECODER[ch])
                else:
                    flush()
                    chunks.append(ch)
        flush()
        return "".join(chunks)

    def get_vocab_size(self):
        return len(self.vocab)

    def token_to_id(self, token):
        return self.vocab.get(token)

    def id_to_token(self, index):
        return self.id_to_token_map.get(index)

    def add_special_tokens(self, tokens):
        for t in tokens:
            if t not in self.vocab:
                new_id = len(self.vocab)
                self.vocab[t] = new_id
                self.id_to_token_map[new_id] = t
            self.added_tokens[t] = self.vocab[t]

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        model = data.get("model", {})
        vocab = model.get("vocab", {})
        merges = {}
        for rank, m in enumerate(model.get("merges", [])):
            pair = tuple(m.split(" ")) if isinstance(m, str) else tuple(m)
            merges[pair] = rank
        unk = model.get("unk_token") or "<unk>"
        tok = cls(vocab=vocab, merges=merges, unk_token=unk)
        for entry in data.get("added_tokens", []):
            content, tid = entry["content"], entry["id"]
            tok.vocab.setdefault(content, tid)
            tok.added_tokens[content] = tid
        tok.id_to_token_map = {v: k for k, v in tok.vocab.items()}
        return tok

    @classmethod
    def from_pretrained(cls, identifier, revision="main", cache_dir=None):
        cache_dir = (
            Path(cache_dir) if cache_dir else Path.home() / ".cache" / "tokenizers_mock"
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = cache_dir / f"{identifier.replace('/', '__')}_{revision}.json"
        if not target.exists():
            url = (
                f"https://huggingface.co/{identifier}/resolve/{revision}/tokenizer.json"
            )
            try:
                urlretrieve(url, target)
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to download tokenizer.json for {identifier!r}: {exc}"
                ) from exc
        return cls.from_file(target)

    def save(self, path, pretty=True):
        path = Path(path)
        ordered_merges = sorted(self.merges, key=self.merges.get)
        data = {
            "model": {
                "type": "BPE",
                "vocab": self.vocab,
                "merges": [f"{a} {b}" for a, b in ordered_merges],
                "unk_token": self.unk_token,
            }
        }
        path.write_text(
            json.dumps(data, indent=2 if pretty else None, ensure_ascii=False),
            encoding="utf-8",
        )
