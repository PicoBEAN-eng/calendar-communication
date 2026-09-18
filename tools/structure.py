"""Structure by parent token (approved 2026-09-18, event 1a8vs5d4dtfvecetvc7uc1tg68).

Position lives in exactly one authored place: the location field, which reads
    <own key>  p<parent key>  [c<class key> ...]      (whole searchable tokens; p/c markers reserved)
The hidden mirror_path is a machine-owned CACHE rebuilt from the parent chain; nobody authors it.
A folder is a folder-note: a file inside its own folder under the folder's name (Garden/Garden.md), whose
body above the first --- divider is a generated listing (a minimal map of contents); anything below the
divider survives regeneration, and the identity key stays the last line. Exactly one root folder-note has
no parent. Notes never contain notes.

Two gestures, one act: move a note = rewrite its p-token; move a folder = rewrite the folder-note's p-token
and every descendant follows, because a child stores only its immediate parent.
The nexus speaks both languages: an Obsidian drag (a file's directory differs from the cache) becomes a
p-token rewrite; a token edit (derived path differs from the cache) becomes a file move. When both moved
different ways in one window the MOST RECENT change wins (event updated vs file ctime) and the other
side is reconciled to it.
Guards: missing parent -> parked under _orphans/ (never deleted, reported); cycle -> refused, parked under
_parked/; a second parentless folder-note -> orphan; the Skyriver chart subtree is pinned (watcher-owned):
a p-token there is refused and reported.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

import links

ROOT_NAME = "Calendar mirror"
FOLDER_DAY = "3000-01-02"          # folder-notes file with the indexes: they are maps of contents
PINNED_PREFIX = "Skyriver/"
ORPHANS, PARKED = "_orphans", "_parked"
UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]+')


def title_name(title: str) -> str:
    return UNSAFE.sub("-", re.sub(r"^Note:?\s*", "", title or "")).strip()


def parse_location(loc: str, own: str | None):
    """(own, parent, classes, others) from a location string; `own` is the identity from the key line."""
    parent, classes, others = None, [], []
    for tok in (loc or "").split():
        if tok == own:
            continue
        if len(tok) == 6 and tok[0] == "p" and links.is_key(tok[1:]):
            parent = tok[1:]
        elif len(tok) == 6 and tok[0] == "c" and links.is_key(tok[1:]):
            classes.append(tok[1:])
        else:
            others.append(tok)
    return own, parent, classes, others


def format_location(own: str, parent: str | None, classes, others) -> str:
    return " ".join([own] + ([f"p{parent}"] if parent else []) + [f"c{c}" for c in classes] + list(others))


class Node:
    __slots__ = ("ev", "key", "parent", "classes", "others", "is_folder", "cache", "derived", "actual", "status")

    def __init__(self, ev):
        self.ev = ev
        priv = ev.get("extendedProperties", {}).get("private", {})
        self.key = links.key_of(ev.get("description") or "")
        _, self.parent, self.classes, self.others = parse_location(ev.get("location"), self.key)
        self.is_folder = priv.get("comms_kind") == "folder"
        self.cache = priv.get("mirror_path")
        self.derived = self.actual = None
        self.status = None

    @property
    def name(self) -> str:
        return title_name(self.ev.get("summary", ""))

    @property
    def pinned(self) -> bool:
        return bool(self.cache and self.cache.startswith(PINNED_PREFIX))


def build(events) -> dict:
    nodes = {}
    for ev in events:
        n = Node(ev)
        if n.key:
            nodes[n.key] = n
    return nodes


def derive(nodes: dict) -> list:
    """Fill node.derived from the parent chain. Returns problems [(node, reason)]."""
    problems = []
    roots = [n for n in nodes.values() if n.is_folder and not n.parent and not n.pinned]
    root = None
    if roots:
        named = [n for n in roots if n.name == ROOT_NAME]
        root = named[0] if named else min(roots, key=lambda n: n.ev.get("created", ""))
        for n in roots[1:]:
            problems.append((n, "second parentless folder-note"))
    memo = {}
    base = root.name if root else ROOT_NAME
    cyclic = {n.key for n in nodes.values() if n.is_folder and not n.pinned and n.parent and _in_cycle(n, nodes)}

    def folder_dir(n: Node, trail):
        """The directory a folder-note owns. Cycle members are frozen where they are (the edit is refused);
        a folder whose parent is missing keeps its directory, under _orphans."""
        if n.key in memo:
            return memo[n.key]
        if n is root:
            memo[n.key] = n.name; return n.name
        if n.key in cyclic or n.key in trail:
            memo[n.key] = str(Path(n.cache).parent) if n.cache else f"{base}/{PARKED}/{n.name}"; return memo[n.key]
        p = nodes.get(n.parent) if n.parent else None
        if p is None or not p.is_folder or p.pinned:
            memo[n.key] = f"{base}/{ORPHANS}/{n.name}"; return memo[n.key]
        d = folder_dir(p, trail | {n.key})
        memo[n.key] = f"{d}/{n.name}"
        return memo[n.key]

    for n in nodes.values():
        if n.pinned:
            n.derived = n.cache
            if n.parent:
                problems.append((n, "p-token refused: the Skyriver chart subtree is pinned (watcher-owned)"))
            continue
        if n.is_folder:
            n.derived = f"{folder_dir(n, frozenset())}/{n.name}.md"
            if n.key in cyclic:
                problems.append((n, "cycle refused: folder frozen where it is until the token is fixed"))
            elif n is not root and (not n.parent or n.parent not in nodes or not nodes[n.parent].is_folder):
                problems.append((n, "parent missing: folder parked under _orphans"))
            continue
        p = nodes.get(n.parent) if n.parent else None
        if p is not None and p.is_folder and not p.pinned:
            n.derived = f"{folder_dir(p, frozenset())}/{n.name}.md"
        elif n.parent:
            n.derived = f"{base}/{ORPHANS}/{n.name}.md"; problems.append((n, "parent missing, parked in orphans"))
        elif n.cache:
            n.derived = n.cache; problems.append((n, "no parent token (legacy path kept)"))
        else:
            n.derived = f"{base}/{ORPHANS}/{n.name}.md"; problems.append((n, "no parent and no path"))
    return problems


def _in_cycle(n: Node, nodes: dict) -> bool:
    seen, cur = set(), n
    while cur and cur.parent:
        if cur.key in seen:
            return True
        seen.add(cur.key)
        cur = nodes.get(cur.parent)
    return False


def scan_vault(vault: Path, prefer: dict | None = None) -> dict:
    """key -> relative path of every keyed .md file in the vault (the drag detector's eyes).
    _conflicts copies are ignored; when a key is found twice the cached path wins, else the first seen."""
    out = {}
    for p in vault.rglob("*.md"):
        if "_conflicts" in p.parts:
            continue
        try:
            k = links.key_of(re.sub(r"%% vault-only %%.*?%% /vault-only %%\n?", "", p.read_text(encoding="utf-8", errors="replace"), flags=re.S))
        except OSError:
            continue
        if k:
            rel = str(p.relative_to(vault))
            if k not in out or (prefer and prefer.get(k) == rel):
                out[k] = rel
    return out


def folder_key_for_dir(rel_dir: str, nodes: dict) -> str | None:
    """The folder-note whose derived directory is rel_dir."""
    for n in nodes.values():
        if n.is_folder and n.derived and str(Path(n.derived).parent) == rel_dir:
            return n.key
    return None


def reconcile(nodes: dict, vault: Path, actual: dict, log) -> list:
    """Decide, per node, what moved: returns actions [(kind, node, arg)] where kind in
    'move-file' (arg = new rel path), 'set-parent' (arg = new parent key), 'cache' (arg = rel path)."""
    actions = []
    order = sorted(nodes.values(), key=lambda n: (not n.is_folder, (n.derived or "").count("/")))
    for n in order:
        if n.pinned or not n.derived:
            continue
        n.actual = actual.get(n.key)
        cache, derived, act = n.cache, n.derived, n.actual
        if act is None:                       # not in the vault (yet): the content pass creates it at derived
            if derived != cache:
                actions.append(("cache", n, derived))
            continue
        if act == derived:
            if cache != act:
                actions.append(("cache", n, act))
            continue
        dragged = act != cache
        token_changed = derived != cache
        if token_changed and not dragged:
            actions.append(("move-file", n, derived)); continue
        new_parent = folder_key_for_dir(str(Path(act).parent), nodes)
        if dragged and not token_changed:
            if new_parent and new_parent != n.key:
                actions.append(("set-parent", n, new_parent))
            else:
                log(f"drag of {n.name} into a folder without a folder-note: moved back to {derived}")
                actions.append(("move-file", n, derived))
            continue
        # both moved, different ways: most recent wins
        ev_t = datetime.fromisoformat(n.ev["updated"].replace("Z", "+00:00"))
        f_t = datetime.fromtimestamp((vault / act).stat().st_ctime, tz=timezone.utc)
        if f_t > ev_t and new_parent and new_parent != n.key:
            log(f"conflict on {n.name}: drag is more recent, token follows")
            actions.append(("set-parent", n, new_parent))
        else:
            log(f"conflict on {n.name}: token is more recent, file follows")
            actions.append(("move-file", n, derived))
    return actions


def listing(node: Node, nodes: dict) -> str:
    """The generated part of a folder-note: its name and its children, folders first, then notes."""
    kids = [c for c in nodes.values() if c.parent == node.key and c.derived and not c.pinned]
    folders = sorted((c for c in kids if c.is_folder), key=lambda c: c.name.lower())
    notes = sorted((c for c in kids if not c.is_folder), key=lambda c: c.name.lower())
    lines = [f"# {node.name}", ""]
    lines += [f"- [[{c.name}]]/" for c in folders]
    lines += [f"- [[{c.name}]]" for c in notes]
    if not kids:
        lines.append("(empty)")
    return "\n".join(lines)


def with_listing(text: str, gen: str, key: str) -> str:
    """Generated listing above the first --- divider; whatever sits below it survives; key stays last."""
    tail = ""
    if "\n---\n" in text:
        tail = text.split("\n---\n", 1)[1]
    elif text.strip():
        tail = text.strip() + "\n"     # first time: the note's own content moves below the divider, intact
    tail = re.sub(r"(?m)^" + re.escape(key) + r"\s*$", "", tail).rstrip()
    return gen.rstrip() + "\n\n---\n" + (tail + "\n\n" if tail else "") + key + "\n"
