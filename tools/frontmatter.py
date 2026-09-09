"""Строгий парсер YAML-подмножества, которое использует канонический формат агента.

Полноценный YAML не нужен: схема наша, она маленькая и фиксированная. Вместо
зависимости — парсер, который понимает ровно нужное и **падает на всём остальном**.
Это осознанный выбор: снисходительный самописный YAML — рассадник тихих багов,
строгий — ещё и дисциплинирует схему.

Поддерживается:
    key: value                  скаляр (хвостовой # комментарий отбрасывается)
    key: >                      свёрнутый блок, продолжение с отступом
    key:                        список из "- item" с отступом
    key:                        вложенная карта из "subkey: value" с отступом
    key: {}                     пустая инлайн-карта

Всё прочее — ошибка.
"""


class FrontmatterError(ValueError):
    pass


def split(text, path="<string>"):
    """Разбивает файл на (frontmatter_raw, body)."""
    if not text.startswith("---\n"):
        raise FrontmatterError(f"{path}: файл должен начинаться с '---'")
    end = text.find("\n---\n", 3)
    if end == -1:
        raise FrontmatterError(f"{path}: не закрыт блок frontmatter")
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
            f"{path}:{lineno}: инлайн-коллекции не поддерживаются (кроме '{{}}'): {raw!r}")
    return v


def parse(raw, path="<string>"):
    lines = raw.split("\n")
    out, i = {}, 0

    while i < len(lines):
        line = lines[i]
        if "\t" in line:
            raise FrontmatterError(f"{path}:{i+1}: табуляция во frontmatter")
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[0] in " ":
            raise FrontmatterError(f"{path}:{i+1}: неожиданный отступ: {line!r}")
        if ":" not in line:
            raise FrontmatterError(f"{path}:{i+1}: ожидалось 'key: ...': {line!r}")

        key, _, rest = line.partition(":")
        key, rest = key.strip(), rest.strip()
        i += 1

        if rest == ">":                                   # свёрнутый блок
            chunk = []
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith("  ")):
                chunk.append(lines[i].strip())
                i += 1
            out[key] = " ".join(c for c in chunk if c)
        elif rest == "":                                  # список или карта
            block = []
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith("  ")):
                if lines[i].strip():
                    block.append((i + 1, lines[i]))
                i += 1
            if not block:
                raise FrontmatterError(f"{path}: пустое значение у '{key}'")
            if all(ln.strip().startswith("- ") for _, ln in block):
                out[key] = [_scalar(ln.strip()[2:], path, n) for n, ln in block]
            else:
                sub = {}
                for n, ln in block:
                    if ":" not in ln:
                        raise FrontmatterError(f"{path}:{n}: ожидалось 'key: value': {ln!r}")
                    k, _, v = ln.strip().partition(":")
                    sub[k.strip()] = _scalar(v, path, n)
                out[key] = sub
        else:
            out[key] = {} if rest == "{}" else _scalar(rest, path, i)

    return out
