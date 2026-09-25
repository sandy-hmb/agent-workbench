"""Errors safe to present at the command boundary."""

class WorkbenchError(ValueError):
    def __init__(self, code: str, message: str, field: str | None = None):
        self.code, self.field = code, field
        super().__init__(f'{code}: {message}')

    def as_dict(self):
        return {'code': self.code, 'message': str(self), **({'field': self.field} if self.field else {})}
