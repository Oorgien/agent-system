class Inexpressible(Exception):
    """The harness cannot express the canonical access boundary.

    Generation must fail instead of producing a configuration with broader access
    than the canonical definition declares. Silently widening access is the worst
    outcome: the definition would claim a boundary that does not actually exist.
    """


class RenderError(Exception):
    """The generated file does not conform to the harness schema.

    Unlike Inexpressible, which means the canonical definition cannot be expressed,
    this indicates an adapter bug. Validate the output because valid syntax
    (TOML parses) does not imply a valid schema (required keys are present
    and the prompt text survived serialization).
    """
