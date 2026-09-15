"""Strict parser for the YAML subset used by the canonical agent format.

Full YAML support is unnecessary: we own a small, fixed schema. Instead of a
new dependency, this parser supports exactly what we need and **rejects everything
else**. This is deliberate: a lenient custom YAML parser invites silent bugs;
a strict one also keeps the schema disciplined.

Supported:
    key: value                  scalar (trailing # comment is discarded)
    key: >                      folded block with indented continuation
    key:                        indented list of "- item" entries
    key:                        indented mapping of "subkey: value" entries
    key: {}                     empty inline mapping

Everything else is an error.
"""


class FrontmatterError(ValueError):
    pass


def split(text, path="<string>"):
    """Split a file into (frontmatter_raw, body)."""
    if not text.startswith("---\n"):
        raise FrontmatterError(f"{path}: file must start with '---'")
    end = text.find("\n---\n", 3)
    if end == -1:
        raise FrontmatterError(f"{path}: unterminated frontmatter block")
    return text[4:end + 1], text[end + 5:]


def _scalar(raw, path, lineno):
    v = raw.strip()
    if v == "{}":
        return {}
    if v and v[0] in "\"'" and v[-1] == v[0] and len(v) > 1:
        return v[1:-1]
    if "#" in v:
        v = v.split("#", 1)[0].strip()
    if v.startswith(("[", "{")):
        raise FrontmatterError(
            f"{path}:{lineno}: inline collections are unsupported (except '{{}}'): {raw!r}")
    return v


def parse(raw, path="<string>"):
    lines = raw.split("\n")
    out, i = {}, 0

    while i < len(lines):
        line = lines[i]
        if "\t" in line:
            raise FrontmatterError(f"{path}:{i+1}: tab character in frontmatter")
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[0] in " ":
            raise FrontmatterError(f"{path}:{i+1}: unexpected indentation: {line!r}")
        if ":" not in line:
            raise FrontmatterError(f"{path}:{i+1}: expected 'key: ...': {line!r}")

        key, _, rest = line.partition(":")
        key, rest = key.strip(), rest.strip()
        i += 1

        if rest == ">":                                   # folded block
            chunk = []
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith("  ")):
                chunk.append(lines[i].strip())
                i += 1
            out[key] = " ".join(c for c in chunk if c)
        elif rest == "":                                  # list or mapping
            block = []
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith("  ")):
                if lines[i].strip():
                    block.append((i + 1, lines[i]))
                i += 1
            if not block:
                raise FrontmatterError(f"{path}: empty value for '{key}'")
            if all(ln.strip().startswith("- ") for _, ln in block):
                out[key] = [_scalar(ln.strip()[2:], path, n) for n, ln in block]
            else:
                sub = {}
                for n, ln in block:
                    if ":" not in ln:
                        raise FrontmatterError(f"{path}:{n}: expected 'key: value': {ln!r}")
                    k, _, v = ln.strip().partition(":")
                    sub[k.strip()] = _scalar(v, path, n)
                out[key] = sub
        else:
            out[key] = {} if rest == "{}" else _scalar(rest, path, i)

    return out
