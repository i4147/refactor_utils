"""Dependency-aware ordering of blocks."""

from __future__ import annotations
import heapq
from collections import defaultdict
from typing import Callable, Mapping, Sequence
from loguru import logger
from .models import Block, ResolverError


def topo_order(
    nodes: Sequence[Block],
    deps: Mapping[int, set[int]],
    rank: Callable[[Block], int],
    key: Callable[[Block], tuple],
    strict: bool = False,
) -> list[Block]:
    """Order blocks so that eager dependencies are emitted first.

    A heap per rank keeps constants before classes before functions while
    respecting dependencies. Import cycles cannot be ordered; the least
    constrained block is emitted instead and a warning logged (or an error
    raised when ``strict`` is set).

    Args:
        nodes: Blocks to order.
        deps: Mapping of block id to the ids of blocks it depends on eagerly.
        rank: Section rank of a block (0 constants, 1 classes, 2 functions).
        key: Full sort key for a block, used to keep the output deterministic.
        strict: Raise :class:`RuntimeError` on circular dependencies instead of
            emitting an arbitrary (and potentially un-importable) order.

    Returns:
        The blocks in dependency order.

    Raises:
        ResolverError: If ``strict`` is set and a cycle must be broken.

    """
    by_id = {b.bid: b for b in nodes}
    pending = {
        b.bid: {d for d in deps.get(b.bid, ()) if d in by_id and d != b.bid}
        for b in nodes
    }
    dependents: dict[int, list[int]] = defaultdict(list)
    for bid, ds in pending.items():
        for d in ds:
            dependents[d].append(bid)
    heaps: dict[int, list] = defaultdict(list)
    queued: set[int] = set()
    done: set[int] = set()
    out: list[Block] = []

    def push(bid: int) -> None:
        b = by_id[bid]
        heapq.heappush(heaps[rank(b)], (key(b), bid))
        queued.add(bid)

    for bid, ds in pending.items():
        if not ds:
            push(bid)
    cur = 0
    while len(done) < len(nodes):
        ranks = [r for r, h in heaps.items() if h]
        if not ranks:
            cand = min(
                (i for i in pending if i not in done and i not in queued),
                key=lambda i: (len(pending[i]), rank(by_id[i]), key(by_id[i])),
            )
            blocked = ", ".join(
                sorted(by_id[d].primary or f"<{by_id[d].kind}>" for d in pending[cand])
            )
            message = (
                f"Circular import-time dependency: emitting "
                f"{by_id[cand].primary or f'<{by_id[cand].kind}>'} "
                f"({by_id[cand].module}) before {blocked}"
            )
            if strict:
                raise ResolverError(message)
            logger.warning(message)
            push(cand)
            continue
        r = cur if heaps[cur] else min(ranks)
        cur = r
        _, bid = heapq.heappop(heaps[r])
        done.add(bid)
        out.append(by_id[bid])
        for dep in dependents[bid]:
            pending[dep].discard(bid)
            if not pending[dep] and dep not in queued and dep not in done:
                push(dep)
    return out
