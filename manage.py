"""Compatibility entrypoint for the administrative command."""

from services.admin import manage as _manage

connect = _manage.connect
_ORIGINAL_CONNECT = _manage.connect


def main():
    # Tests and local tooling historically patch ``manage.connect``. Mirror
    # that binding into the implementation before dispatching the command.
    _manage.connect = connect
    try:
        return _manage.main()
    finally:
        _manage.connect = _ORIGINAL_CONNECT


if __name__ == "__main__":
    main()
