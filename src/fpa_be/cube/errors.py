class CubeError(Exception):
    """Base class for cube-read failures."""


class UnknownVintageError(CubeError):
    def __init__(self, vintage: int):
        super().__init__(f"no such vintage: {vintage}")
        self.vintage = vintage


class NoVintageClosedYetError(CubeError):
    def __init__(self, as_of: str):
        super().__init__(f"no vintage had closed at or before {as_of!r}")
        self.as_of = as_of
