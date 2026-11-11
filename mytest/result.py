"""Consolidated from ``mytest`` by pyrefactor (generated file)."""

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.request import urlretrieve


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


__version__ = "0.15.0"
__m2_version__ = "0.0.1-mock"

_PRETOKENIZE = re.compile(
    r"""'s|'t|'re|'ve|'m|'ll|'d| ?[^\W\d_]+| ?\d+| ?[^\s\w]+|\s+(?!\S)|\s+"""
)


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------


class AddedToken:
    def __init__(
        self,
        content,
        single_word=False,
        lstrip=False,
        rstrip=False,
        normalized=True,
        special=False,
    ):
        self.content = content
        self.single_word = single_word
        self.lstrip = lstrip
        self.rstrip = rstrip
        self.normalized = normalized
        self.special = special

    def __repr__(self):
        return f"AddedToken('{self.content}')"


@dataclass
class Encoding:
    ids = field(default_factory=list)
    tokens = field(default_factory=list)
    offsets = field(default_factory=list)
    attention_mask = field(default_factory=list)
    special_tokens_mask = field(default_factory=list)
    type_ids = field(default_factory=list)
    overflowing = field(default_factory=list)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.ids[key]
        raise TypeError(f"Invalid key type: {type(key)}")

    def __len__(self):
        return len(self.ids)

    def __repr__(self):
        return f"Encoding(num_tokens={len(self)}, tokens={self.tokens[:5]}...)"

    def truncate(self, max_length, stride=0, direction="right"):
        if direction == "right":
            self.ids = self.ids[:max_length]
            self.tokens = self.tokens[:max_length]
            self.offsets = self.offsets[:max_length]
            if self.attention_mask:
                self.attention_mask = self.attention_mask[:max_length]
            if self.special_tokens_mask:
                self.special_tokens_mask = self.special_tokens_mask[:max_length]
            if self.type_ids:
                self.type_ids = self.type_ids[:max_length]
        else:
            self.ids = self.ids[-max_length:]
            self.tokens = self.tokens[-max_length:]
            self.offsets = self.offsets[-max_length:]
            if self.attention_mask:
                self.attention_mask = self.attention_mask[-max_length:]
            if self.special_tokens_mask:
                self.special_tokens_mask = self.special_tokens_mask[-max_length:]
            if self.type_ids:
                self.type_ids = self.type_ids[-max_length:]

    def pad(
        self, length, pad_id=0, pad_token="[PAD]", pad_type_id=0, direction="right"
    ):
        padding_length = length - len(self.ids)
        if padding_length <= 0:
            return
        if direction == "right":
            self.ids.extend([pad_id] * padding_length)
            self.tokens.extend([pad_token] * padding_length)
            self.offsets.extend([(0, 0)] * padding_length)
            if self.attention_mask:
                self.attention_mask.extend([0] * padding_length)
            if self.special_tokens_mask:
                self.special_tokens_mask.extend([0] * padding_length)
            if self.type_ids:
                self.type_ids.extend([pad_type_id] * padding_length)
        else:
            self.ids = [pad_id] * padding_length + self.ids
            self.tokens = [pad_token] * padding_length + self.tokens
            self.offsets = [(0, 0)] * padding_length + self.offsets
            if self.attention_mask:
                self.attention_mask = [0] * padding_length + self.attention_mask
            if self.special_tokens_mask:
                self.special_tokens_mask = [
                    0
                ] * padding_length + self.special_tokens_mask
            if self.type_ids:
                self.type_ids = [pad_type_id] * padding_length + self.type_ids


class models:
    class BPE:
        def __init__(
            self,
            vocab=None,
            merges=None,
            unk_token=None,
            fuse_unk=False,
        ):
            self.vocab = vocab or {}
            self.reverse_vocab = {v: k for k, v in self.vocab.items()}
            self.merges = merges or []
            self.unk_token = unk_token
            self.fuse_unk = fuse_unk

        def save(self, path, prefix=None):
            os.makedirs(
                os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True
            )
            with open(path, "w") as f:
                json.dump({"vocab": self.vocab, "merges": self.merges}, f)

    class WordPiece:
        def __init__(self, vocab=None, unk_token="[UNK]", max_input_chars_per_word=100):
            self.vocab = vocab or {}
            self.reverse_vocab = {v: k for k, v in self.vocab.items()}
            self.unk_token = unk_token
            self.max_input_chars_per_word = max_input_chars_per_word

    class WordLevel:
        def __init__(self, vocab=None, unk_token="[UNK]"):
            self.vocab = vocab or {}
            self.reverse_vocab = {v: k for k, v in self.vocab.items()}
            self.unk_token = unk_token

    class Unigram:
        def __init__(self, vocab=None, unk_id=None):
            self.vocab = [(token, prob) for token, prob in (vocab or [])]
            self.unk_id = unk_id


class trainers:
    class BpeTrainer:
        def __init__(
            self,
            vocab_size=30000,
            min_frequency=2,
            show_progress=True,
            special_tokens=None,
            limit_alphabet=1000,
            initial_alphabet=None,
            continuing_subword_prefix=None,
            end_of_word_suffix=None,
        ):
            self.vocab_size = vocab_size
            self.min_frequency = min_frequency
            self.show_progress = show_progress
            self.special_tokens = special_tokens or []
            self.limit_alphabet = limit_alphabet
            self.initial_alphabet = initial_alphabet or []
            self.continuing_subword_prefix = continuing_subword_prefix
            self.end_of_word_suffix = end_of_word_suffix

    class WordPieceTrainer:
        def __init__(
            self,
            vocab_size=30000,
            min_frequency=2,
            show_progress=True,
            special_tokens=None,
            limit_alphabet=1000,
            initial_alphabet=None,
            continuing_subword_prefix="##",
            end_of_word_suffix=None,
        ):
            self.vocab_size = vocab_size
            self.min_frequency = min_frequency
            self.show_progress = show_progress
            self.special_tokens = special_tokens or []
            self.limit_alphabet = limit_alphabet
            self.initial_alphabet = initial_alphabet or []
            self.continuing_subword_prefix = continuing_subword_prefix
            self.end_of_word_suffix = end_of_word_suffix

    class WordLevelTrainer:
        def __init__(
            self,
            vocab_size=30000,
            min_frequency=2,
            show_progress=True,
            special_tokens=None,
        ):
            self.vocab_size = vocab_size
            self.min_frequency = min_frequency
            self.show_progress = show_progress
            self.special_tokens = special_tokens or []

    class UnigramTrainer:
        def __init__(
            self,
            vocab_size=8000,
            show_progress=True,
            special_tokens=None,
            shrinking_factor=0.75,
            unk_token=None,
            max_piece_length=16,
            n_sub_iterations=2,
        ):
            self.vocab_size = vocab_size
            self.show_progress = show_progress
            self.special_tokens = special_tokens or []
            self.shrinking_factor = shrinking_factor
            self.unk_token = unk_token
            self.max_piece_length = max_piece_length
            self.n_sub_iterations = n_sub_iterations


class decoders:
    class BPEDecoder:
        def __init__(self, suffix="</w>"):
            self.suffix = suffix

    class WordPiece:
        def __init__(self, prefix="##", cleanup=True):
            self.prefix = prefix
            self.cleanup = cleanup

    class Metaspace:
        def __init__(self, replacement="▁", add_prefix_space=True):
            self.replacement = replacement
            self.add_prefix_space = add_prefix_space

    class ByteLevel:
        def __init__(self):
            pass

    class CTC:
        def __init__(
            self,
            pad_token="<pad>",
            word_delimiter_token="|",
            cleanup=True,
        ):
            self.pad_token = pad_token
            self.word_delimiter_token = word_delimiter_token
            self.cleanup = cleanup


class normalizers:
    class NFD:
        pass

    class NFKD:
        pass

    class NFC:
        pass

    class NFKC:
        pass

    class Lowercase:
        pass

    class Strip:
        def __init__(self, left=True, right=True):
            self.left = left
            self.right = right

    class StripAccents:
        pass

    class Replace:
        def __init__(self, pattern, content):
            self.pattern = pattern
            self.content = content

    class BertNormalizer:
        def __init__(
            self,
            clean_text=True,
            handle_chinese_chars=True,
            strip_accents=True,
            lowercase=False,
        ):
            self.clean_text = clean_text
            self.handle_chinese_chars = handle_chinese_chars
            self.strip_accents = strip_accents
            self.lowercase = lowercase

    class Sequence:
        def __init__(self, normalizers_list):
            self.normalizers = normalizers_list


class pre_tokenizers:
    class ByteLevel:
        def __init__(self, add_prefix_space=True, use_regex=True):
            self.add_prefix_space = add_prefix_space
            self.use_regex = use_regex

    class Whitespace:
        def __init__(self):
            pass

    class WhitespaceSplit:
        pass

    class BertPreTokenizer:
        pass

    class Metaspace:
        def __init__(self, replacement="▁", add_prefix_space=True):
            self.replacement = replacement
            self.add_prefix_space = add_prefix_space

    class CharDelimiterSplit:
        def __init__(self, delimiter):
            self.delimiter = delimiter

    class Digits:
        def __init__(self, individual_digits=False):
            self.individual_digits = individual_digits

    class Punctuation:
        def __init__(self, behavior="isolated"):
            self.behavior = behavior

    class Sequence:
        def __init__(self, pretokenizers_list):
            self.pretokenizers = pretokenizers_list


class post_processors:
    class TemplateProcessing:
        def __init__(
            self,
            single,
            pair,
            special_tokens=None,
        ):
            self.single = single
            self.pair = pair
            self.special_tokens = special_tokens or []

    class ByteLevel:
        def __init__(self, trim_offsets=True):
            self.trim_offsets = trim_offsets

    class BertProcessing:
        def __init__(self, sep, cls):
            self.sep = sep
            self.cls = cls

    class RobertaProcessing:
        def __init__(self, sep, cls, trim_offsets=True, add_prefix_space=True):
            self.sep = sep
            self.cls = cls
            self.trim_offsets = trim_offsets
            self.add_prefix_space = add_prefix_space


class Tokenizer:
    def __init__(self, model):
        self.model = model
        self._normalizer = None
        self._pre_tokenizer = None
        self._post_processor = None
        self._decoder = None
        self._added_tokens = {}
        self._padding = None
        self._truncation = None

    @classmethod
    def from_file(cls, path):
        if path.endswith(".json"):
            with open(path, "r") as f:
                data = json.load(f)
            if "model" in data and "vocab" in data["model"]:
                vocab = data["model"]["vocab"]
                merges = data["model"].get("merges", [])
                tokenizer = cls(models.BPE(vocab=vocab, merges=merges))
            else:
                tokenizer = cls(models.BPE())
            return tokenizer
        else:
            return cls(models.Unigram())

    def from_pretrained(self, identifier, **kwargs):
        print(f"Mock loading pretrained tokenizer: {identifier}")
        if "bert" in identifier.lower():
            vocab = {"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3, "[MASK]": 4}
            return self.__class__(models.BPE(vocab=vocab))
        elif "gpt" in identifier.lower():
            vocab = {"<|endoftext|>": 0, "!": 1, '"': 2}
            return self.__class__(models.BPE(vocab=vocab))
        else:
            return self.__class__(models.BPE())

    def train(self, files, trainer=None):
        print(f"Mock training tokenizer on {len(files)} files")

    def train_from_iterator(self, iterator, trainer=None, length=None):
        print("Mock training tokenizer from iterator")

    def encode(
        self,
        sequence,
        pair=None,
        is_pretokenized=False,
        add_special_tokens=True,
    ):
        if isinstance(sequence, list):
            encodings = []
            for seq in sequence:
                encodings.append(self._encode_single(seq, add_special_tokens))
            return encodings
        else:
            return self._encode_single(sequence, add_special_tokens)

    def _encode_single(self, sequence, add_special_tokens=True):
        words = sequence.split()
        tokens = []
        ids = []
        offsets = []
        current_offset = 0
        for word in words:
            if len(word) > 5:
                pieces = [word[:3], word[3:]]
            else:
                pieces = [word]
            for piece in pieces:
                pos = sequence.find(piece, current_offset)
                if pos != -1:
                    current_offset = pos + len(piece)
                    offsets.append((pos, current_offset))
                else:
                    offsets.append((current_offset, current_offset + len(piece)))
                tokens.append(piece)
                ids.append(hash(piece) % 30000)
        encoding = Encoding(
            ids=ids, tokens=tokens, offsets=offsets, attention_mask=[1] * len(ids)
        )
        if add_special_tokens and self.model.vocab:
            for token, token_id in self.model.vocab.items():
                if token in ["[CLS]", "<s>"]:
                    encoding.ids = [token_id] + encoding.ids
                    encoding.tokens = [token] + encoding.tokens
                    encoding.offsets = [(0, 0)] + encoding.offsets
                    encoding.attention_mask = [1] + encoding.attention_mask
                elif token in ["[SEP]", "</s>"]:
                    encoding.ids.append(token_id)
                    encoding.tokens.append(token)
                    encoding.offsets.append((0, 0))
                    encoding.attention_mask.append(1)
        return encoding

    def decode(self, ids, skip_special_tokens=True):
        if self.model.reverse_vocab:
            tokens = [self.model.reverse_vocab.get(id, "<unk>") for id in ids]
        else:
            tokens = [f"<token_{id}>" for id in ids]
        if skip_special_tokens:
            tokens = [
                t for t in tokens if not t.startswith("[") and not t.startswith("<")
            ]
        return " ".join(tokens)

    def encode_batch(self, sequences, is_pretokenized=False, add_special_tokens=True):
        return [
            self.encode(seq, add_special_tokens=add_special_tokens) for seq in sequences
        ]

    def save(self, path, pretty=True):
        os.makedirs(
            os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True
        )
        data = {
            "version": "1.0",
            "model": {
                "type": "BPE",
                "vocab": self.model.vocab if hasattr(self.model, "vocab") else {},
                "merges": self.model.merges if hasattr(self.model, "merges") else [],
            },
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2 if pretty else None)
        print(f"Mock saved tokenizer to {path}")

    def add_special_tokens(self, tokens):
        added_count = 0
        for token in tokens:
            if isinstance(token, AddedToken):
                token_str = token.content
            else:
                token_str = token
            if hasattr(self.model, "vocab") and token_str not in self.model.vocab:
                new_id = len(self.model.vocab)
                self.model.vocab[token_str] = new_id
                self.model.reverse_vocab[new_id] = token_str
                added_count += 1
        return added_count

    def add_tokens(self, tokens):
        return self.add_special_tokens(tokens)

    def get_vocab(self, with_added_tokens=True):
        if hasattr(self.model, "vocab"):
            return self.model.vocab.copy()
        return {}

    def get_vocab_size(self, with_added_tokens=True):
        return len(self.get_vocab(with_added_tokens))

    def enable_padding(
        self,
        direction="right",
        pad_id=0,
        pad_type_id=0,
        pad_token="[PAD]",
        length=None,
    ):
        self._padding = {
            "direction": direction,
            "pad_id": pad_id,
            "pad_type_id": pad_type_id,
            "pad_token": pad_token,
            "length": length,
        }

    def enable_truncation(
        self, max_length, stride=0, strategy="longest_first", direction="right"
    ):
        self._truncation = {
            "max_length": max_length,
            "stride": stride,
            "strategy": strategy,
            "direction": direction,
        }

    def no_padding(self):
        self._padding = None

    def no_truncation(self):
        self._truncation = None

    def __repr__(self):
        return f"Tokenizer(vocab_size={self.get_vocab_size()})"


class m2_Encoding:
    __slots__ = ("ids", "tokens", "attention_mask")

    def __init__(self, ids, tokens, attention_mask):
        self.ids = ids
        self.tokens = tokens
        self.attention_mask = attention_mask

    def __repr__(self):
        return f"Encoding(num_tokens={len(self.ids)})"


class m2_Tokenizer:
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
        return m2_Encoding(ids=ids, tokens=tokens, attention_mask=[1] * len(ids))

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


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def from_file(path):
    return Tokenizer.from_file(path)


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


def _get_ranked_pairs(word, merges):
    pairs = list(zip(word, word[1:]))
    return [(merges[p], p) for p in pairs if p in merges]


_BYTE_ENCODER = _bytes_to_unicode()
_BYTE_DECODER = {v: k for k, v in _BYTE_ENCODER.items()}
