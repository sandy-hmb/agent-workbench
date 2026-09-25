"""Small input checks shared by controlled work-item commands."""
from workbench.errors import WorkbenchError
import argparse


class CommandParser(argparse.ArgumentParser):
    def error(self, message):
        raise WorkbenchError('ARGUMENT_INVALID', message, '$args')


def object_fields(value, *, allowed=None, required=(), field='$'):
    if not isinstance(value, dict):
        raise WorkbenchError('INPUT_INVALID', '必须是对象', field)
    missing = set(required) - set(value)
    extra = set(value) - set(allowed) if allowed is not None else set()
    if missing or extra:
        name = sorted(missing or extra)[0]
        raise WorkbenchError('INPUT_INVALID', '缺少必填字段' if missing else '未知字段', field + '.' + name)
    return value


def text(value, field, *, empty=False):
    if not isinstance(value, str) or '\0' in value or (not empty and not value.strip()):
        raise WorkbenchError('INPUT_INVALID', '必须是非空文字' if not empty else '必须是文字', field)
    return value


def array(value, field):
    if not isinstance(value, list):
        raise WorkbenchError('INPUT_INVALID', '必须是数组', field)
    return value
