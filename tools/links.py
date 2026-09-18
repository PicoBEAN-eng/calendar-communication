"""Identity keys and link translation for the calendar <-> vault mirror (2026-09-18).

Identity: every mirrored note ends with one bare 5-character key (a-z0-9, first char a letter, at least
one digit) on its own last line — the same line on both sides, so the key travels with the text.
Links: the vault writes [[Name]] (or [[path/Name|alias]], [[Name#heading]]). On the way UP the target's
key is glued after the closing brackets — [[Name]]l<key> — so the calendar copy points at a thing by
identity; on the way DOWN the glue is stripped and the name refreshed to the key owner's current name,
so Obsidian sees a normal wiki-link. down(up(x)) == x for any x that carries no glue itself.
A link whose target has no key on either side stays name-only (counted, never invented).
"""
import random, re, string

KEY = re.compile(r"^(?=[a-z0-9]*\d)[a-z][a-z0-9]{4}$")
# `\|` is the alias separator inside tables; the backslash belongs to the alias part, not the name.
LINK = re.compile(r"\[\[([^\]\|#]+?)((?:#[^\]\|]*)?(?:\\?\|[^\]]*)?)\]\]")
GLUED = re.compile(r"\[\[([^\]\|#]+?)((?:#[^\]\|]*)?(?:\\?\|[^\]]*)?)\]\]l([a-z][a-z0-9]{4})\b")


def is_key(tok: str) -> bool:
    return bool(KEY.match(tok or ""))


def mint(taken: set) -> str:
    while True:
        k = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(5))
        if is_key(k) and k[0] not in "pc" and k not in taken:   # p/c are reserved marker letters
            taken.add(k)
            return k


def key_of(text: str) -> str | None:
    """The identity key: the last non-empty line, if it is a bare key."""
    lines = [ln.strip() for ln in (text or "").rstrip().splitlines() if ln.strip()]
    return lines[-1] if lines and is_key(lines[-1]) else None


def with_key(text: str, key: str) -> str:
    if key_of(text) == key:
        return text
    return text.rstrip() + "\n\n" + key + "\n"


def basename(target: str) -> str:
    return target.strip().rsplit("/", 1)[-1]


def up(text: str, name2key: dict, missing: set | None = None) -> str:
    def sub(m):
        key = name2key.get(basename(m.group(1)))
        if not key:
            if missing is not None:
                missing.add(basename(m.group(1)))
            return m.group(0)
        return f"{m.group(0)}l{key}"
    return LINK.sub(sub, text)


def down(text: str, key2name: dict) -> str:
    def sub(m):
        target, rest, key = m.group(1), m.group(2), m.group(3)
        name = key2name.get(key)
        if name is None:
            return m.group(0)      # unknown key: left visible on purpose (a tripwire)
        if basename(target) != name:
            head = target[: len(target) - len(basename(target))]
            target = head + name
        return f"[[{target}{rest}]]"
    return GLUED.sub(sub, text)
